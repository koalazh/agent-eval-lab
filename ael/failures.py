from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .cases import CaseSpec, ExperimentSpec, SuiteSpec, load_case
from .hashing import canonical_json, hash_file_tree, sha256_text
from .models import AgentVariant, FailureStatus
from .persistence import Repository


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
    verifier = run.get("verifier") or {}
    verifier_text = " ".join(
        str(verifier.get(key) or "") for key in ("stdout", "stderr", "error")
    )
    verifier_text = re.sub(r"/(?:private/)?var/folders/[^\s:]+", "<workspace>", verifier_text)
    verifier_text = re.sub(r"/Users/[^\s:]+", "<repo>", verifier_text)
    verifier_text = re.sub(r"line \d+", "line", verifier_text)
    verifier_text = re.sub(r"\b\d+(?:\.\d+)?(?:ms|s)\b", "<duration>", verifier_text)
    verifier_text = re.sub(
        r"\b\d+\s+(passed|failed|deselected|errors?)\b",
        r"<count> \1",
        verifier_text,
    )
    verifier_text = re.sub(r"\s+", " ", verifier_text).strip()
    details = {
        "source_run_id": run_id,
        "experiment_id": run["experiment_id"],
        "case_id": run["case_id"],
        "case_revision": run["case_revision"],
        "variant_id": run["variant_id"],
        "verifier_signature_text": verifier_text,
        "run_status": run["run_status"],
        "task_outcome": run["task_outcome"],
        "run_dir": run["run_dir"],
        "coverage": run.get("evidence_coverage", {}),
        "workspace": changes,
    }
    signature = sha256_text(
        canonical_json(
            {
                "case_id": run["case_id"],
                "case_revision": run["case_revision"],
                "task_outcome": run["task_outcome"],
                "verifier": verifier_text,
            }
        )
    )
    failure_id = repository.upsert_failure_cluster(
        signature=signature,
        source_run_id=run_id,
        details=details,
    )
    return failure_id


def promote_failure(repository: Repository, failure_id: str) -> CaseSpec:
    failure = repository.get_failure(failure_id)
    if not failure:
        raise ValueError(f"未找到失败记录：{failure_id}")
    source_run = repository.get_run(failure["source_run_id"])
    if not source_run:
        raise ValueError("源 Run 不可用")
    source_case = repository.get_case(source_run["case_id"], source_run["case_revision"])
    if not source_case:
        raise ValueError("源 Case revision 不可用")
    if hash_file_tree(source_case.fixture_path) != source_case.fixture_hash:
        raise ValueError("source fixture 已不再匹配持久化的 Case revision")
    if source_case.source_path and source_case.source_path.exists():
        current_case = load_case(source_case.source_path)
        if current_case.revision != source_case.revision:
            raise ValueError("source Case 已不再匹配持久化的 revision")
    repository.append_suite_case("regression", "regression", source_case)
    details = dict(failure["details"])
    details["regression_case_id"] = source_case.id
    details["regression_case_revision"] = source_case.revision
    repository.update_failure_details(failure_id, details)
    repository.update_failure_status(failure_id, FailureStatus.REGRESSION_GUARDED)
    return source_case


def reconcile_follow_up(repository: Repository, experiment_id: str, source_run_id: str) -> str | None:
    failure = repository.failure_for_run(source_run_id)
    source_run = repository.get_run(source_run_id)
    definition = repository.read_experiment_definition(experiment_id) or {}
    metadata = definition.get("metadata") or {}
    baseline_id = metadata.get("baseline_variant_id")
    candidate_id = metadata.get("candidate_variant_id")
    if not failure or not source_run or not baseline_id or not candidate_id:
        return None

    def relevant_runs(variant_id: str) -> list[dict[str, Any]]:
        return [
            run
            for run in repository.list_runs(experiment_id)
            if run["variant_id"] == variant_id
            and run["case_id"] == source_run["case_id"]
            and run["case_revision"] == source_run["case_revision"]
            and run["run_status"] == "COMPLETED"
        ]

    baseline_runs = relevant_runs(str(baseline_id))
    candidate_runs = relevant_runs(str(candidate_id))
    if not baseline_runs or not candidate_runs:
        return None
    baseline_outcomes = [run["task_outcome"] for run in baseline_runs]
    candidate_outcomes = [run["task_outcome"] for run in candidate_runs]
    source_outcome = failure["details"].get("task_outcome") or source_run.get("task_outcome")
    transition = None
    if source_outcome == "FAIL" and all(outcome == "FAIL" for outcome in baseline_outcomes) and all(
        outcome == "PASS" for outcome in candidate_outcomes
    ):
        transition = "FIXED"
        status = FailureStatus.FIXED
    elif source_outcome == "PASS" and all(outcome == "PASS" for outcome in baseline_outcomes) and all(
        outcome == "FAIL" for outcome in candidate_outcomes
    ):
        transition = "REGRESSED"
        status = FailureStatus.REPRODUCED
    else:
        return None

    details = dict(failure["details"])
    details.update(
        {
            "follow_up_experiment_id": experiment_id,
            "baseline_variant_id": baseline_id,
            "candidate_variant_id": candidate_id,
            "baseline_outcomes": baseline_outcomes,
            "candidate_outcomes": candidate_outcomes,
            "case_id": source_run["case_id"],
            "case_revision": source_run["case_revision"],
            "discriminating_experiment": True,
            "lifecycle_transition": transition,
        }
    )
    repository.update_failure_details(failure["id"], details)
    repository.update_failure_status(failure["id"], status)
    return transition


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
        raise ValueError("回归套件为空")
    return ExperimentSpec(
        id=experiment_id,
        suite=SuiteSpec("regression", "regression", tuple(cases)),
        variants=variants,
        trials=max(1, trials),
        max_concurrency=max(1, max_concurrency),
        metadata={"source": "Failure Book regression suite"},
    )
