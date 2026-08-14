from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any, Iterable

from ..models import Agent, Capabilities, DriverResult, ObservableEvent, RunContext
from ..process import ProcessSupervisor


def _run_help(binary: str, *args: str) -> tuple[str, str, int | None]:
    try:
        result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return "", str(exc), None
    return result.stdout, result.stderr, result.returncode


def _version(binary: str) -> str:
    stdout, stderr, _ = _run_help(binary, "--version")
    text = (stdout or stderr).strip()
    return text.splitlines()[0] if text else "UNKNOWN"


def _json_lines(text: str) -> Iterable[dict[str, Any]]:
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def _nested_text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, list):
        parts = [item for item in (_nested_text(v) for v in value) if item]
        return "".join(parts) if parts else None
    if isinstance(value, dict):
        for key in ("text", "content", "message", "delta", "result"):
            text = _nested_text(value.get(key))
            if text:
                return text
    return None


def _normalize(raw: dict[str, Any], source: str) -> ObservableEvent:
    raw_type = str(raw.get("type") or raw.get("method") or raw.get("event") or "")
    lowered = raw_type.lower()
    payload = raw.get("params") if isinstance(raw.get("params"), dict) else raw
    nested_item = raw.get("item")
    if isinstance(nested_item, dict):
        payload = nested_item
        raw_type = str(nested_item.get("type") or raw_type)
        lowered = raw_type.lower()
    message = raw.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").lower()
            if block_type in {"tool_use", "tool_call", "function_call"}:
                return ObservableEvent(
                    kind="tool_call",
                    name=str(block.get("name") or block.get("tool_name") or "tool"),
                    summary="tool call",
                    source=source,
                    data=raw,
                )
            if block_type in {"tool_result", "tool_output", "function_result"}:
                return ObservableEvent(
                    kind="tool_result",
                    name=str(block.get("name") or block.get("tool_name") or "tool"),
                    summary="tool result",
                    source=source,
                    data=raw,
                )
    if "command" in lowered or "exec" in lowered or "bash" in lowered:
        kind = "command"
    elif "file" in lowered or "patch" in lowered or "edit" in lowered:
        kind = "file_change"
    elif "tool" in lowered and ("result" in lowered or "end" in lowered):
        kind = "tool_result"
    elif "tool" in lowered or "function" in lowered:
        kind = "tool_call"
    elif "message" in lowered or "text" in lowered or "delta" in lowered or lowered in {"assistant", "user"}:
        kind = "message"
    elif "turn" in lowered or "agent_end" in lowered or "complete" in lowered or "result" in lowered:
        kind = "final"
    else:
        kind = "unknown"
    name = payload.get("name") or payload.get("toolName") or payload.get("command") or payload.get("path")
    summary = payload.get("summary") or payload.get("command") or payload.get("text") or _nested_text(payload.get("message"))
    return ObservableEvent(
        kind=kind,
        name=str(name) if name else None,
        summary=str(summary) if summary else None,
        source=source,
        data=raw,
    )


class BinaryAgentDriver:
    agent_id = ""
    display_name = ""
    binary = ""

    def __init__(self):
        self._probed: Capabilities | None = None
        self._agent: Agent | None = None

    def probe(self) -> Capabilities:
        if self._probed is not None:
            return self._probed
        path = shutil.which(self.binary)
        if not path:
            self._probed = Capabilities(available=False, notes=(f"{self.binary} not found on PATH",))
            self._agent = Agent(
                self.agent_id,
                self.display_name,
                self.agent_id,
                self.binary,
                "UNKNOWN",
                self._probed.to_dict(),
            )
            return self._probed
        version = _version(path)
        self._probed = self._capabilities(version)
        self._agent = Agent(
            self.agent_id,
            self.display_name,
            self.agent_id,
            path,
            version,
            self._probed.to_dict(),
        )
        return self._probed

    def _capabilities(self, version: str) -> Capabilities:
        return Capabilities(
            available=True,
            version=version,
            supports_models=True,
            supports_controlled=False,
            controlled_support="partial",
        )

    def agent(self) -> Agent:
        self.probe()
        assert self._agent is not None
        return self._agent

    def normalize_native_event(self, raw: object) -> ObservableEvent | None:
        if not isinstance(raw, dict):
            return ObservableEvent("unknown", source="native", data={"raw": str(raw)})
        return _normalize(raw, "native")

    def _binary_path(self) -> str:
        self.probe()
        return self._agent.binary if self._agent else self.binary


class CodexDriver(BinaryAgentDriver):
    agent_id = "codex"
    display_name = "Codex CLI"
    binary = "codex"

    def _capabilities(self, version: str) -> Capabilities:
        return Capabilities(
            available=True,
            version=version,
            supports_models=True,
            supports_controlled=True,
            controlled_support="full",
            supports_telemetry=False,
            supports_deep=True,
            notes=("current CLI help exposes exec --json and per-run config/sandbox flags",),
        )

    async def execute(self, run_context: RunContext, process_supervisor: ProcessSupervisor) -> DriverResult:
        command = [
            self._binary_path(),
            "exec",
            "--json",
            "--cd",
            str(run_context.workspace),
            "--ephemeral",
            "--skip-git-repo-check",
        ]
        if run_context.variant.model != "UNKNOWN":
            command.extend(["--model", run_context.variant.model])
        if run_context.variant.run_mode.value == "controlled":
            command.extend(["--ignore-user-config", "--ignore-rules", "--sandbox", "workspace-write"])
        result = await process_supervisor.run(
            command,
            cwd=run_context.workspace,
            env=run_context.env,
            timeout_seconds=run_context.timeout_seconds,
            stdin=run_context.case_prompt,
        )
        events = [self.normalize_native_event(item) for item in _json_lines(result.stdout)]
        events = [event for event in events if event is not None]
        usage: dict[str, Any] = {}
        session_id = None
        final_text = None
        for item in _json_lines(result.stdout):
            usage.update(item.get("usage") if isinstance(item.get("usage"), dict) else {})
            session_id = session_id or item.get("thread_id") or item.get("session_id")
            final_text = final_text or _nested_text(item.get("message")) or _nested_text(item.get("result"))
        return DriverResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            native_events=events,
            usage=usage,
            session_id=str(session_id) if session_id else None,
            final_text=final_text,
            process_error=result.error,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
        )


class ClaudeCodeDriver(BinaryAgentDriver):
    agent_id = "claude-code"
    display_name = "Claude Code"
    binary = "claude"

    def _capabilities(self, version: str) -> Capabilities:
        return Capabilities(
            available=True,
            version=version,
            supports_models=True,
            supports_controlled=True,
            controlled_support="full",
            supports_telemetry=True,
            supports_deep=True,
            notes=("current CLI help exposes print, stream-json, safe-mode, and no-session-persistence",),
        )

    async def execute(self, run_context: RunContext, process_supervisor: ProcessSupervisor) -> DriverResult:
        env = dict(run_context.env)
        if run_context.observation_profile.value in {"telemetry", "deep"}:
            env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
            if env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
                env["OTEL_METRICS_EXPORTER"] = "otlp"
                env["OTEL_LOGS_EXPORTER"] = "otlp"
            if run_context.observation_profile.value == "deep":
                env["OTEL_LOG_USER_PROMPTS"] = "1"
                env["OTEL_LOG_TOOL_DETAILS"] = "1"
                env["OTEL_LOG_TOOL_CONTENT"] = "1"
        command = [
            self._binary_path(),
            "-p",
            run_context.case_prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--add-dir",
            str(run_context.workspace),
        ]
        if run_context.variant.model != "UNKNOWN":
            command.extend(["--model", run_context.variant.model])
        permission_mode = run_context.variant.harness_config.get("permission_mode")
        if permission_mode:
            command.extend(["--permission-mode", str(permission_mode)])
        if run_context.variant.run_mode.value == "controlled":
            command.extend(["--safe-mode"])
        result = await process_supervisor.run(
            command,
            cwd=run_context.workspace,
            env=env,
            timeout_seconds=run_context.timeout_seconds,
        )
        events = [self.normalize_native_event(item) for item in _json_lines(result.stdout)]
        events = [event for event in events if event is not None]
        usage: dict[str, Any] = {}
        session_id = None
        final_text = None
        for item in _json_lines(result.stdout):
            usage.update(item.get("usage") if isinstance(item.get("usage"), dict) else {})
            session_id = session_id or item.get("session_id")
            final_text = final_text or _nested_text(item.get("result")) or _nested_text(item.get("message"))
        return DriverResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            native_events=events,
            usage=usage,
            session_id=str(session_id) if session_id else None,
            final_text=final_text,
            process_error=result.error,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
        )


class PiDriver(BinaryAgentDriver):
    agent_id = "pi"
    display_name = "Pi"
    binary = "pi"

    def _capabilities(self, version: str) -> Capabilities:
        return Capabilities(
            available=True,
            version=version,
            supports_models=True,
            supports_controlled=True,
            controlled_support="partial",
            supports_telemetry=False,
            supports_deep=True,
            notes=("RPC protocol is current native JSONL; controlled context-file isolation depends on current CLI flags",),
        )

    async def execute(self, run_context: RunContext, process_supervisor: ProcessSupervisor) -> DriverResult:
        command = [self._binary_path(), "--mode", "rpc", "--no-session"]
        if run_context.variant.model != "UNKNOWN":
            if run_context.variant.provider != "UNKNOWN":
                command.extend(["--provider", run_context.variant.provider])
            command.extend(["--model", run_context.variant.model])
        if run_context.variant.run_mode.value == "controlled":
            command.append("--no-context-files")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(run_context.workspace),
                env=run_context.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return DriverResult(exit_code=None, process_error=str(exc))
        assert process.stdin and process.stdout and process.stderr
        stderr_task = asyncio.create_task(process.stderr.read())
        raw_lines: list[str] = []
        events: list[ObservableEvent] = []
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        session_id = None

        async def collect() -> None:
            nonlocal session_id
            process.stdin.write((json.dumps({"type": "prompt", "message": run_context.case_prompt}) + "\n").encode())
            await process.stdin.drain()
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                raw_lines.append(line.decode("utf-8", errors="replace").rstrip("\r\n"))
                try:
                    item = json.loads(raw_lines[-1])
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                events.append(_normalize(item, "native"))
                delta = item.get("assistantMessageEvent") or {}
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text_parts.append(str(delta.get("delta") or ""))
                if isinstance(item.get("usage"), dict):
                    usage.update(item["usage"])
                session_id = session_id or item.get("sessionId") or item.get("session_id")
                if item.get("type") == "agent_settled" or (
                    item.get("type") == "agent_end" and not item.get("willRetry", False)
                ):
                    break

        try:
            await asyncio.wait_for(collect(), timeout=max(1, run_context.timeout_seconds))
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    process.kill()
                await process.wait()
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                process.kill()
            await process.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace")
            return DriverResult(
                exit_code=process.returncode,
                stdout="\n".join(raw_lines),
                stderr=stderr,
                native_events=events,
                usage=usage,
                final_text="".join(text_parts) or None,
                session_id=str(session_id) if session_id else None,
                timed_out=True,
            )
        stderr = (await stderr_task).decode("utf-8", errors="replace")
        return DriverResult(
            exit_code=process.returncode,
            stdout="\n".join(raw_lines),
            stderr=stderr,
            native_events=events,
            usage=usage,
            final_text="".join(text_parts) or None,
            session_id=str(session_id) if session_id else None,
        )


class HermesDriver(BinaryAgentDriver):
    agent_id = "hermes"
    display_name = "Hermes"
    binary = "hermes"

    def _capabilities(self, version: str) -> Capabilities:
        return Capabilities(
            available=True,
            version=version,
            supports_models=True,
            supports_controlled=True,
            controlled_support="partial",
            supports_telemetry=False,
            supports_deep=True,
            notes=("oneshot and usage-file are current; oneshot isolation flags require version-specific validation",),
        )

    async def execute(self, run_context: RunContext, process_supervisor: ProcessSupervisor) -> DriverResult:
        usage_file = Path(run_context.evidence_dir) / "hermes-usage.json"
        command = [
            self._binary_path(),
            "-z",
            run_context.case_prompt,
            "--usage-file",
            str(usage_file),
        ]
        if run_context.variant.model != "UNKNOWN":
            command.extend(["--model", run_context.variant.model])
        if run_context.variant.provider != "UNKNOWN":
            command.extend(["--provider", run_context.variant.provider])
        if run_context.variant.run_mode.value == "controlled":
            command.extend(["--ignore-user-config", "--ignore-rules", "--safe-mode"])
        result = await process_supervisor.run(
            command,
            cwd=run_context.workspace,
            env=run_context.env,
            timeout_seconds=run_context.timeout_seconds,
        )
        usage: dict[str, Any] = {}
        if usage_file.exists():
            try:
                loaded = json.loads(usage_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    usage = loaded
            except json.JSONDecodeError:
                pass
        if usage.get("failed") is True:
            return DriverResult(
                exit_code=1,
                stdout=result.stdout,
                stderr=result.stderr,
                usage=usage,
                process_error="Hermes usage report marked the run failed",
                timed_out=result.timed_out,
                cancelled=result.cancelled,
            )
        events = [
            ObservableEvent("message", name="hermes", summary="oneshot output", source="native"),
            ObservableEvent("final", name="hermes", summary="completed", source="native"),
        ]
        return DriverResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            native_events=events,
            usage=usage,
            final_text=result.stdout,
            session_id=str(usage.get("session_id")) if usage.get("session_id") else None,
            process_error=result.error,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
        )
