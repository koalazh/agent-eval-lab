from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..execution import make_execution_receipt
from ..models import Agent, Capabilities, DriverResult, ObservableEvent, RunContext
from ..process import ProcessSupervisor


class GenericCLIDriver:
    """Execute a persisted local CLI Variant without a plugin or adapter framework."""

    name = "generic-cli"

    def __init__(self) -> None:
        self._agent = Agent(
            id="generic-cli",
            display_name="Generic CLI Harness",
            driver="generic-cli",
            binary="generic-cli",
            detected_version="configured",
            capabilities={
                "available": True,
                "supports_models": False,
                "supports_controlled": False,
                "controlled_support": "partial",
                "supports_telemetry": False,
                "supports_deep": False,
                "notes": ["Executable、arguments、prompt transport 与环境增量由持久 Variant 提供。"],
            },
        )

    def probe(self) -> Capabilities:
        return Capabilities(
            available=True,
            version="configured",
            supports_models=False,
            supports_controlled=False,
            controlled_support="partial",
            notes=("Executable、arguments、prompt transport 与环境增量由持久 Variant 提供。",),
        )

    def agent(self) -> Agent:
        return self._agent

    def normalize_native_event(self, raw: object) -> ObservableEvent | None:
        if not isinstance(raw, dict):
            return ObservableEvent("unknown", source="native", data={"raw": str(raw)})
        kind = str(raw.get("kind") or raw.get("type") or "unknown").lower()
        if "command" in kind:
            normalized = "command"
        elif "file" in kind or "edit" in kind:
            normalized = "file_change"
        elif "tool" in kind and "result" in kind:
            normalized = "tool_result"
        elif "tool" in kind:
            normalized = "tool_call"
        elif "message" in kind or "text" in kind:
            normalized = "message"
        elif "complete" in kind or kind == "final":
            normalized = "final"
        else:
            normalized = "unknown"
        return ObservableEvent(
            normalized,
            name=str(raw.get("name") or raw.get("command") or "") or None,
            summary=str(raw.get("summary") or raw.get("command") or raw.get("text") or "") or None,
            source="native",
            data=raw,
        )

    @staticmethod
    def _resolve_executable(value: str) -> str:
        path = Path(value).expanduser()
        if path.is_absolute() or "/" in value:
            resolved = path.resolve()
            if not resolved.is_file():
                raise ValueError(f"Generic CLI executable 不存在：{value}")
            return str(resolved)
        resolved = shutil.which(value)
        if not resolved:
            raise ValueError(f"Generic CLI executable 不可执行：{value}")
        return resolved

    def _execution(self, run_context: RunContext) -> tuple[list[str], dict[str, str], str | None, dict[str, Any]]:
        variant = run_context.variant
        executable = self._resolve_executable(variant.executable)
        prompt = run_context.case_prompt
        arguments = [str(item) for item in variant.arguments]
        argv = [executable]
        for argument in arguments:
            argv.append(
                argument.replace("{workspace}", str(run_context.workspace)).replace("{prompt}", prompt)
            )
        transport = variant.prompt_transport
        has_prompt_placeholder = any("{prompt}" in argument for argument in arguments)
        if transport == "argument" and not has_prompt_placeholder:
            argv.append(prompt)
        elif transport not in {"stdin", "argument"}:
            raise ValueError(f"Generic CLI prompt transport 不支持：{transport}")
        env = dict(run_context.env)
        env.update({str(key): str(value) for key, value in variant.env_delta.items()})
        stdin = prompt if transport == "stdin" else None
        receipt = make_execution_receipt(
            argv=argv,
            cwd=run_context.workspace,
            prompt=prompt,
            prompt_transport=transport,
            env=env,
            resolved_executable=executable,
        )
        return argv, env, stdin, receipt

    async def execute(self, run_context: RunContext, process_supervisor: ProcessSupervisor) -> DriverResult:
        argv, env, stdin, receipt = self._execution(run_context)
        result = await process_supervisor.run(
            argv,
            cwd=run_context.workspace,
            env=env,
            timeout_seconds=run_context.timeout_seconds,
            stdin=stdin,
        )
        events: list[ObservableEvent] = []
        for line in result.stdout.splitlines():
            try:
                raw: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            normalized = self.normalize_native_event(raw)
            if normalized:
                events.append(normalized)
        return DriverResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            native_events=events,
            process_error=result.error,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            final_text=result.stdout[-4000:] if result.stdout else None,
            execution_receipt=receipt,
        )
