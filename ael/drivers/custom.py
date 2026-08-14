from __future__ import annotations

import json
from typing import Any

from ..models import Agent, Capabilities, DriverResult, ObservableEvent, RunContext
from ..process import ProcessSupervisor


class CustomCLIDriver:
    def __init__(self, agent: Agent, command: list[str]):
        self.name = agent.id
        self._agent = agent
        self.command = command

    def probe(self) -> Capabilities:
        return Capabilities(
            available=True,
            version=self._agent.detected_version,
            supports_models=True,
            supports_controlled=False,
            controlled_support="partial",
            notes=("custom command；隔离语义由该命令自身负责。",),
        )

    def agent(self) -> Agent:
        return self._agent

    async def execute(self, run_context: RunContext, process_supervisor: ProcessSupervisor) -> DriverResult:
        result = await process_supervisor.run(
            self.command,
            cwd=run_context.workspace,
            env=run_context.env,
            timeout_seconds=run_context.timeout_seconds,
            stdin=run_context.case_prompt,
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
        )

    def normalize_native_event(self, raw: object) -> ObservableEvent | None:
        if not isinstance(raw, dict):
            return ObservableEvent("unknown", source="native", data={"raw": str(raw)})
        kind = str(raw.get("kind") or raw.get("type") or "unknown").lower()
        if "command" in kind:
            normalized = "command"
        elif "file" in kind:
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
