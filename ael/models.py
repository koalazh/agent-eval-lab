from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    TIMEOUT = "TIMEOUT"
    PROCESS_ERROR = "PROCESS_ERROR"
    CANCELLED = "CANCELLED"
    INVALID = "INVALID"
    VERIFIER_ERROR = "VERIFIER_ERROR"


class TaskOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class FailureStatus(str, Enum):
    OBSERVED = "OBSERVED"
    REPRODUCED = "REPRODUCED"
    FIXED = "FIXED"
    REGRESSION_GUARDED = "REGRESSION_GUARDED"


class RunMode(str, Enum):
    NATIVE = "native"
    CONTROLLED = "controlled"


class ObservationProfile(str, Enum):
    MINIMAL = "minimal"
    TELEMETRY = "telemetry"
    DEEP = "deep"


class ComparisonConfidence(str, Enum):
    CONTROLLED = "CONTROLLED"
    PARTIAL = "PARTIAL"
    DESCRIPTIVE = "DESCRIPTIVE"


@dataclass(frozen=True)
class Capabilities:
    available: bool
    version: str = "UNKNOWN"
    supports_models: bool = False
    supports_controlled: bool = False
    controlled_support: str = "UNKNOWN"
    supports_telemetry: bool = False
    supports_deep: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "version": self.version,
            "supports_models": self.supports_models,
            "supports_controlled": self.supports_controlled,
            "controlled_support": self.controlled_support,
            "supports_telemetry": self.supports_telemetry,
            "supports_deep": self.supports_deep,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Agent:
    id: str
    display_name: str
    driver: str
    binary: str
    detected_version: str = "UNKNOWN"
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "driver": self.driver,
            "binary": self.binary,
            "detected_version": self.detected_version,
            "capabilities": dict(self.capabilities),
        }


@dataclass(frozen=True)
class AgentVariant:
    id: str
    agent_id: str
    model: str = "UNKNOWN"
    provider: str = "UNKNOWN"
    model_config: Mapping[str, Any] = field(default_factory=dict)
    harness_config: Mapping[str, Any] = field(default_factory=dict)
    run_mode: RunMode = RunMode.NATIVE
    observation_profile: ObservationProfile = ObservationProfile.MINIMAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "model": self.model,
            "provider": self.provider,
            "model_config": dict(self.model_config),
            "harness_config": dict(self.harness_config),
            "run_mode": self.run_mode.value,
            "observation_profile": self.observation_profile.value,
        }


@dataclass(frozen=True)
class ObservableEvent:
    kind: str
    name: str | None = None
    summary: str | None = None
    source: str = "native"
    raw_ref: str | None = None
    timestamp: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "summary": self.summary,
            "source": self.source,
            "raw_ref": self.raw_ref,
            "timestamp": self.timestamp,
            "data": dict(self.data),
        }


@dataclass
class RunContext:
    run_id: str
    experiment_id: str
    case_id: str
    variant: AgentVariant
    trial: int
    workspace: Any
    evidence_dir: Any
    timeout_seconds: int
    env: dict[str, str]
    observation_profile: ObservationProfile
    case_prompt: str = ""


@dataclass
class DriverResult:
    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    native_events: list[ObservableEvent] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    final_text: str | None = None
    session_id: str | None = None
    process_error: str | None = None
    timed_out: bool = False
    cancelled: bool = False

    @property
    def completed_process(self) -> bool:
        return (
            not self.timed_out
            and not self.cancelled
            and self.process_error is None
            and self.exit_code == 0
        )


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None

