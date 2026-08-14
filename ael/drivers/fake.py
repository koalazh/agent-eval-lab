from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..models import Agent, Capabilities, DriverResult, ObservableEvent, RunContext
from ..process import ProcessSupervisor


class FakeAgentDriver:
    def __init__(self, agent_id: str, behavior: str = "pass"):
        self.name = agent_id
        self.behavior = behavior
        capabilities = Capabilities(
            available=True,
            version="fake-1",
            supports_models=True,
            supports_controlled=True,
            controlled_support="full",
            notes=("deterministic test driver",),
        )
        self._agent = Agent(
            id=agent_id,
            display_name=f"Fake {agent_id}",
            driver="fake",
            binary=agent_id,
            detected_version="fake-1",
            capabilities=capabilities.to_dict(),
        )

    def probe(self) -> Capabilities:
        return Capabilities(
            available=True,
            version="fake-1",
            supports_models=True,
            supports_controlled=True,
            controlled_support="full",
            notes=("deterministic test driver",),
        )

    def agent(self) -> Agent:
        return self._agent

    async def execute(self, run_context: RunContext, process_supervisor: ProcessSupervisor) -> DriverResult:
        behavior = str(run_context.variant.harness_config.get("fake_behavior", self.behavior))
        if behavior == "flaky":
            behavior = "pass" if run_context.trial % 2 == 1 else "fail"
        if behavior == "timeout":
            await asyncio.sleep(run_context.timeout_seconds + 2)
        if behavior == "crash":
            return DriverResult(exit_code=1, process_error="fake process crashed", stderr="fake crash")
        events = [
            ObservableEvent("message", name="fake", summary="started", source="native"),
            ObservableEvent("tool_call", name="bash", summary="test -f answer.txt", source="native"),
        ]
        answer = "pass\n" if behavior in {"pass", "jsonl"} else "fail\n"
        (Path(run_context.workspace) / "answer.txt").write_text(answer, encoding="utf-8")
        events.append(ObservableEvent("file_change", name="answer.txt", summary="updated", source="native"))
        if behavior == "jsonl":
            raw = [
                {"type": "command_execution", "command": "pytest -q"},
                {"type": "file_change", "path": "answer.txt"},
                {"type": "turn_complete", "success": True},
            ]
            (Path(run_context.evidence_dir) / "fake-native.jsonl").write_text(
                "\n".join(json.dumps(item) for item in raw) + "\n", encoding="utf-8"
            )
        events.extend(
            [
                ObservableEvent("command", name="pytest", summary="pytest -q", source="native"),
                ObservableEvent("final", name="fake", summary="done", source="native"),
            ]
        )
        return DriverResult(
            exit_code=0,
            stdout="fake agent completed",
            native_events=events,
            final_text="done",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            session_id=f"fake-{run_context.run_id}",
        )

    def normalize_native_event(self, raw: object) -> ObservableEvent | None:
        if not isinstance(raw, dict):
            return ObservableEvent("unknown", source="native", data={"raw": str(raw)})
        kind = str(raw.get("type") or "unknown")
        mapping = {"command_execution": "command", "file_change": "file_change", "turn_complete": "final"}
        return ObservableEvent(
            mapping.get(kind, "unknown"),
            name=str(raw.get("command") or raw.get("path") or "") or None,
            summary=str(raw.get("command") or raw.get("path") or "") or None,
            source="native",
            data=raw,
        )

