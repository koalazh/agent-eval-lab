from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .hashing import canonical_json, hash_file_tree, sha256_text


@dataclass(frozen=True)
class VerifierSpec:
    command: str | None = None
    python: str | None = None

    @property
    def kind(self) -> str:
        if self.command:
            return "command"
        if self.python:
            return "python"
        return "unknown"

    def to_dict(self) -> dict[str, str]:
        if self.command:
            return {"command": self.command}
        if self.python:
            return {"python": self.python}
        return {}


@dataclass(frozen=True)
class CaseSpec:
    id: str
    prompt: str
    fixture_path: Path
    verifier: VerifierSpec
    timeout_seconds: int = 600
    constraints: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None
    revision: str = ""
    fixture_hash: str = ""

    def __post_init__(self) -> None:
        if not self.fixture_hash:
            object.__setattr__(self, "fixture_hash", hash_file_tree(self.fixture_path))
        if not self.revision:
            verifier_fingerprint = self.verifier.to_dict()
            if self.verifier.python:
                verifier_path = Path(self.verifier.python)
                if not verifier_path.is_absolute() and self.source_path:
                    verifier_path = self.source_path.parent / verifier_path
                verifier_fingerprint = {
                    **verifier_fingerprint,
                    "content_hash": hash_file_tree(verifier_path),
                }
            payload = {
                "id": self.id,
                "prompt": self.prompt,
                "fixture_hash": self.fixture_hash,
                "verifier": verifier_fingerprint,
                "constraints": self.constraints,
                "timeout_seconds": self.timeout_seconds,
            }
            object.__setattr__(self, "revision", sha256_text(canonical_json(payload)))

    @property
    def prompt_hash(self) -> str:
        return sha256_text(self.prompt)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "fixture_path": str(self.fixture_path),
            "verifier": self.verifier.to_dict(),
            "timeout_seconds": self.timeout_seconds,
            "constraints": self.constraints,
            "source_path": str(self.source_path) if self.source_path else None,
            "revision": self.revision,
            "fixture_hash": self.fixture_hash,
        }


@dataclass(frozen=True)
class SuiteSpec:
    id: str
    kind: str
    cases: tuple[CaseSpec, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "cases": [case.id for case in self.cases],
            "case_revisions": [{"id": case.id, "revision": case.revision} for case in self.cases],
        }


@dataclass(frozen=True)
class ExperimentSpec:
    id: str
    suite: SuiteSpec
    variants: tuple[Any, ...]
    trials: int = 1
    max_concurrency: int = 1
    source_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "suite": self.suite.to_dict(),
            "variants": [variant.to_dict() for variant in self.variants],
            "trials": self.trials,
            "max_concurrency": self.max_concurrency,
            "source_path": str(self.source_path) if self.source_path else None,
            "metadata": self.metadata,
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 必须包含 mapping")
    return raw


def load_case(path: str | Path) -> CaseSpec:
    source = Path(path).resolve()
    raw = _load_yaml(source)
    fixture_raw = raw.get("fixture")
    fixture_value = fixture_raw.get("path") if isinstance(fixture_raw, dict) else fixture_raw
    if not fixture_value:
        raise ValueError(f"{source}：必须配置 fixture.path")
    fixture = (source.parent / str(fixture_value)).resolve()
    verify_raw = raw.get("verify") or {}
    if not isinstance(verify_raw, dict):
        raise ValueError(f"{source}：verify 必须是 mapping")
    verifier = VerifierSpec(command=verify_raw.get("command"), python=verify_raw.get("python"))
    if verifier.kind == "unknown":
        raise ValueError(f"{source}：必须配置 verify.command 或 verify.python")
    limits = raw.get("limits") or {}
    return CaseSpec(
        id=str(raw.get("id") or source.parent.name),
        prompt=str(raw.get("prompt") or ""),
        fixture_path=fixture,
        verifier=verifier,
        timeout_seconds=int(limits.get("timeout_seconds", 600)),
        constraints=dict(raw.get("constraints") or {}),
        source_path=source,
    )


def discover_case_paths(root: str | Path) -> list[Path]:
    """Find case definitions that the Web builder can offer without YAML editing."""
    base = Path(root).resolve()
    candidates = [
        *base.glob("examples/cases/*/case.yaml"),
        *base.glob("cases/*/case.yaml"),
        *base.glob("cases/*/*/case.yaml"),
    ]
    discovered: set[Path] = set()
    for path in candidates:
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            continue
        discovered.add(resolved)
    return sorted(discovered)


def _variant_from_raw(raw: dict[str, Any], index: int):
    from .models import AgentVariant, ObservationProfile, RunMode

    agent_id = str(raw.get("agent") or raw.get("agent_id") or raw.get("driver") or "")
    if not agent_id:
        raise ValueError(f"variant {index}：必须配置 agent")
    variant_id = str(raw.get("id") or f"{agent_id}-{raw.get('model', 'UNKNOWN')}-{index}")
    return AgentVariant(
        id=variant_id,
        agent_id=agent_id,
        name=str(raw.get("name") or ""),
        executable=str(raw.get("executable") or ""),
        subject_revision=str(raw.get("subject_revision") or "UNKNOWN"),
        agent_version=str(raw.get("agent_version") or "UNKNOWN"),
        model=str(raw.get("model") or "UNKNOWN"),
        provider=str(raw.get("provider") or "UNKNOWN"),
        model_config=dict(raw.get("model_config") or {}),
        harness_config=dict(raw.get("config") or raw.get("harness_config") or {}),
        arguments=tuple(str(item) for item in (raw.get("arguments") or ())),
        prompt_transport=str(raw.get("prompt_transport") or "stdin"),
        env_delta={str(key): str(value) for key, value in dict(raw.get("env_delta") or {}).items()},
        version_command=tuple(str(item) for item in (raw.get("version_command") or ())),
        run_mode=RunMode(str(raw.get("run_mode") or "native")),
        observation_profile=ObservationProfile(str(raw.get("observation_profile") or "minimal")),
    )


def load_experiment(path: str | Path, case_root: str | Path | None = None) -> ExperimentSpec:
    source = Path(path).resolve()
    raw = _load_yaml(source)
    suite_raw = raw.get("suite")
    suite_id = str(suite_raw.get("id") if isinstance(suite_raw, dict) else suite_raw or raw.get("id") or source.stem)
    suite_kind = str((suite_raw.get("kind") if isinstance(suite_raw, dict) else None) or raw.get("suite_kind") or "development")
    case_values = raw.get("cases") or (suite_raw.get("cases") if isinstance(suite_raw, dict) else None) or []
    if not case_values:
        raise ValueError(f"{source}：实验或 suite mapping 中必须配置 cases")
    root = Path(case_root).resolve() if case_root else source.parent
    cases = []
    for value in case_values:
        case_path = Path(str(value))
        if not case_path.is_absolute():
            case_path = root / case_path
        cases.append(load_case(case_path))
    variants = tuple(_variant_from_raw(item, index) for index, item in enumerate(raw.get("variants") or []))
    if not variants:
        raise ValueError(f"{source}：必须配置 variants")
    return ExperimentSpec(
        id=str(raw.get("id") or source.stem),
        suite=SuiteSpec(suite_id, suite_kind, tuple(cases)),
        variants=variants,
        trials=max(1, int(raw.get("trials", 1))),
        max_concurrency=max(1, int(raw.get("max_concurrency", 1))),
        source_path=source,
        metadata=dict(raw.get("metadata") or {}),
    )
