from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from .cases import CaseSpec, ExperimentSpec, SuiteSpec
from .hashing import canonical_json, sha256_text
from .models import AgentVariant, FailureStatus
from .persistence import Repository


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "case"


def observe_failure(repository: Repository, run_id: str) -> str | None:
    run = repository.get_run(run_id)
    if not run or run["run_status"] != "COMPLETED" or run["task_outcome"] != "FAIL":
        return None
    workspace = Path(run["run_dir"]) / "workspace" / "changes.json"
    changes: dict[str, Any] = {}
    if workspace.exists():
        try:
            changes = json.loads(workspace.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            changes = {}
    details = {
        "source_run_id": run_id,
        "case_id": run["case_id"],
        "case_revision": run["case_revision"],
        "run_status": run["run_status"],
        "task_outcome": run["task_outcome"],
        "run_dir": run["run_dir"],
        "coverage": run.get("evidence_coverage", {}),
        "workspace": changes,
    }
    signature = sha256_text(
        canonical_json(
            {
                "case_revision": run["case_revision"],
                "changed_files": changes.get("changed_files", []),
                "task_outcome": run["task_outcome"],
            }
        )
    )
    failure_id = f"failure-{run_id}"
    repository.save_failure(failure_id, run_id, signature, details)
    return failure_id


def promote_failure(repository: Repository, failure_id: str) -> CaseSpec:
    failure = repository.get_failure(failure_id)
    if not failure:
        raise ValueError(f"failure not found: {failure_id}")
    source_run = repository.get_run(failure["source_run_id"])
    if not source_run:
        raise ValueError("source Run is unavailable")
    source_case = repository.get_case(source_run["case_id"], source_run["case_revision"])
    if not source_case:
        raise ValueError("source Case revision is unavailable")
    target_id = f"regression-{_safe_name(source_case.id)}-{failure_id.removeprefix('failure-')[:8]}"
    case_dir = repository.root / "cases" / "regression" / target_id
    fixture_dir = case_dir / "fixture"
    case_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source_case.fixture_path, fixture_dir)
    verifier = source_case.verifier.to_dict()
    if source_case.verifier.python:
        grader_source = Path(source_case.verifier.python)
        if not grader_source.is_absolute() and source_case.source_path:
            grader_source = source_case.source_path.parent / grader_source
        grader_target = case_dir / grader_source.name
        shutil.copy2(grader_source, grader_target)
        verifier = {"python": grader_target.name}
    case_yaml = {
        "id": target_id,
        "fixture": {"path": "fixture"},
        "prompt": source_case.prompt,
        "verify": verifier,
        "constraints": source_case.constraints,
        "limits": {"timeout_seconds": source_case.timeout_seconds},
    }
    source_yaml = case_dir / "case.yaml"
    source_yaml.write_text(yaml.safe_dump(case_yaml, sort_keys=False), encoding="utf-8")
    from .cases import load_case

    promoted = load_case(source_yaml)
    repository.save_case(promoted)
    repository.append_suite_case("regression", "regression", promoted)
    details = dict(failure["details"])
    details["regression_case_id"] = promoted.id
    repository.update_failure_details(failure_id, details)
    repository.update_failure_status(failure_id, FailureStatus.REGRESSION_GUARDED)
    return promoted


def build_regression_experiment(
    repository: Repository,
    *,
    experiment_id: str,
    variants: tuple[AgentVariant, ...],
    trials: int = 1,
    max_concurrency: int = 1,
) -> ExperimentSpec:
    cases = repository.suite_cases("regression")
    if not cases:
        raise ValueError("regression suite is empty")
    return ExperimentSpec(
        id=experiment_id,
        suite=SuiteSpec("regression", "regression", tuple(cases)),
        variants=variants,
        trials=max(1, trials),
        max_concurrency=max(1, max_concurrency),
        metadata={"source": "Failure Book regression suite"},
    )

