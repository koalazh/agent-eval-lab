from __future__ import annotations

from typing import Protocol

from ..models import Agent, Capabilities, DriverResult, ObservableEvent, RunContext
from ..process import ProcessSupervisor


class AgentDriver(Protocol):
    name: str

    def probe(self) -> Capabilities:
        ...

    def agent(self) -> Agent:
        ...

    async def execute(self, run_context: RunContext, process_supervisor: ProcessSupervisor) -> DriverResult:
        ...

    def normalize_native_event(self, raw: object) -> ObservableEvent | None:
        ...

