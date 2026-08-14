from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .hashing import UNKNOWN, canonical_json
from .persistence import Repository
from .reports import matrix_report, trial_summary


_FINGERPRINT_FIELDS = (
    ("agent_id", "Agent"),
    ("agent_version", "Agent version"),
    ("driver", "Driver"),
    ("driver_version", "Driver version"),
    ("model", "Model"),
    ("provider", "Provider"),
    ("model_config", "Model config"),
    ("harness_config_hash", "Harness config"),
    ("run_mode", "Run mode"),
    ("observation_profile", "Observation profile"),
    ("runtime", "Runtime"),
    ("case_revision", "Case revision"),
    ("prompt_hash", "Prompt"),
    ("fixture_hash", "Fixture"),
    ("ael_version", "AEL version"),
    ("git_sha", "Git SHA"),
)


def _value(fingerprint: dict[str, Any], field: str) -> Any:
    return fingerprint.get(field, UNKNOWN)


def variable_scope(
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> dict[str, Any]:
    same: list[str] = []
    changed: list[str] = []
    unknown: list[str] = []
    for field, label in _FINGERPRINT_FIELDS:
        left = _value(candidate, field)
        right = _value(reference, field)
        if left == UNKNOWN or right == UNKNOWN or left is None or right is None:
            unknown.append(label)
        elif canonical_json(left) == canonical_json(right):
            same.append(label)
        else:
            changed.append(label)
    return {"same": same, "changed": changed, "unknown": unknown}


def _expected_scope(scope: dict[str, Any]) -> str:
    changed = set(scope["changed"])
    if changed == {"Model"}:
        return "Model Differential"
    if changed and changed <= {"Agent", "Driver", "Agent version", "Driver version"}:
        return "Harness Differential"
    if changed == {"Harness config"}:
        return "Feature Differential"
    if changed == {"Agent version", "Driver version"}:
        return "Version Differential"
    return "Descriptive"


def comparison_confidence(scope: dict[str, Any]) -> str:
    if scope["unknown"]:
        return "PARTIAL"
    changed = set(scope["changed"])
    if not changed:
        return "CONTROLLED"
    if changed in (
        {"Model"},
        {"Harness config"},
        {"Agent", "Driver"},
        {"Agent version", "Driver version"},
    ):
        return "CONTROLLED"
    return "DESCRIPTIVE"


def _events(repository: Repository, run: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(run["run_dir"]) / "native" / "events.jsonl"
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _anchor(event: dict[str, Any]) -> str | None:
    kind = str(event.get("kind") or "unknown")
    name = str(event.get("name") or "")
    summary = str(event.get("summary") or "")
    text = f"{name} {summary}".strip().lower()
    if kind in {"tool_call", "tool_result", "command", "file_change", "verification", "final"}:
        return f"{kind}:{text}".strip(":")
    if any(token in text for token in ("test", "pytest", "verify", "check")):
        return f"{kind}:{text}".strip(":")
    return None


def first_meaningful_divergence(
    candidate_events: list[dict[str, Any]],
    reference_events: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate = [item for item in (_anchor(event) for event in candidate_events) if item]
    reference = [item for item in (_anchor(event) for event in reference_events) if item]
    for index, (left, right) in enumerate(zip(candidate, reference)):
        if left != right:
            return {
                "status": "DIVERGENCE",
                "anchor_index": index,
                "candidate": left,
                "reference": right,
            }
    if len(candidate) != len(reference):
        index = min(len(candidate), len(reference))
        return {
            "status": "DIVERGENCE",
            "anchor_index": index,
            "candidate": candidate[index] if index < len(candidate) else None,
            "reference": reference[index] if index < len(reference) else None,
        }
    return {"status": "NO_CLEAR_DIVERGENCE"}


def _match_score(candidate: dict[str, Any], reference: dict[str, Any]) -> int:
    scope = variable_scope(candidate["fingerprint"], reference["fingerprint"])
    return len(scope["same"]) - (len(scope["unknown"]) * 2)


def matched_pass_run(repository: Repository, candidate: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        run
        for run in repository.list_runs()
        if run["id"] != candidate["id"]
        and run["case_id"] == candidate["case_id"]
        and run["case_revision"] == candidate["case_revision"]
        and run["task_outcome"] == "PASS"
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda run: (_match_score(candidate, run), run["started_at"]), reverse=True)
    selected = candidates[0]
    scope = variable_scope(candidate["fingerprint"], selected["fingerprint"])
    important_equal = len(scope["same"]) >= 3
    return {
        "run": selected,
        "scope": scope,
        "sufficient": important_equal,
        "score": _match_score(candidate, selected),
    }

def artifact_diff(repository: Repository, run: dict[str, Any]) -> dict[str, Any]:
    root = Path(run["run_dir"]) / "workspace"
    result: dict[str, Any] = {}
    for name in ("changes.json", "untracked.json"):
        path = root / name
        if path.exists():
            try:
                result[name.removesuffix(".json")] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                result[name.removesuffix(".json")] = {}
    diff_path = root / "diff.patch"
    result["diff"] = diff_path.read_text(encoding="utf-8", errors="replace") if diff_path.exists() else ""
    return result


def compare_run_details(repository: Repository, run_id: str) -> dict[str, Any]:
    candidate = repository.get_run(run_id)
    if not candidate:
        return {}
    match = matched_pass_run(repository, candidate)
    result: dict[str, Any] = {
        "run_id": run_id,
        "candidate": {
            "status": candidate["run_status"],
            "outcome": candidate["task_outcome"],
            "case_revision": candidate["case_revision"],
            "coverage": candidate.get("evidence_coverage", {}),
        },
        "artifact_diff": artifact_diff(repository, candidate),
        "evidence_coverage": candidate.get("evidence_coverage", {}),
    }
    if not match or not match["sufficient"]:
        result.update(
            {
                "matched_reference": None,
                "variable_scope": None,
                "timeline_diff": {"status": "INSUFFICIENT_REFERENCE"},
                "first_meaningful_divergence": {"status": "NO_CLEAR_DIVERGENCE"},
                "note": "没有足够接近且 revision 相同的 PASS reference；不会进行轨迹归因。",
            }
        )
        return result
    reference = match["run"]
    result.update(
        {
            "matched_reference": {
                "run_id": reference["id"],
                "outcome": reference["task_outcome"],
                "score": match["score"],
            },
            "variable_scope": match["scope"],
            "timeline_diff": {
                "candidate": [_anchor(item) for item in _events(repository, candidate) if _anchor(item)],
                "reference": [_anchor(item) for item in _events(repository, reference) if _anchor(item)],
            },
            "first_meaningful_divergence": first_meaningful_divergence(
                _events(repository, candidate),
                _events(repository, reference),
            ),
            "note": "轨迹证据仅用于描述，不建立因果根因。",
        }
    )
    return result


def _groups(runs: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        groups[(run["case_id"], run["case_revision"], run["variant_id"])].append(run)
    return groups


def _transition(left: dict[str, Any], right: dict[str, Any]) -> str:
    left_class = left["classification"]
    right_class = right["classification"]
    if left_class == "STABLE_FAIL" and right_class == "STABLE_PASS":
        return "FIXED"
    if left_class == "STABLE_PASS" and right_class == "STABLE_FAIL":
        return "REGRESSED"
    if left_class == "FLAKY" and right_class == "STABLE_PASS":
        return "IMPROVED"
    if left_class == "STABLE_PASS" and right_class == "FLAKY":
        return "DEGRADED"
    if left_class == "STABLE_FAIL" and right_class == "FLAKY":
        return "IMPROVED"
    if left_class == "FLAKY" and right_class == "STABLE_FAIL":
        return "DEGRADED"
    if left_class == right_class:
        return "UNCHANGED"
    return "DESCRIPTIVE_CHANGE"


def compare_experiments(repository: Repository, experiment_a: str, experiment_b: str) -> dict[str, Any]:
    runs_a = repository.list_runs(experiment_a)
    runs_b = repository.list_runs(experiment_b)
    groups_a = _groups(runs_a)
    groups_b = _groups(runs_b)
    rows: list[dict[str, Any]] = []
    used_b: set[tuple[str, str, str]] = set()
    for key_a, values_a in sorted(groups_a.items()):
        case_id, revision, variant_id = key_a
        candidate_keys = [
            key_b
            for key_b in groups_b
            if key_b[0] == case_id and key_b[1] == revision
        ]
        if not candidate_keys:
            continue
        key_b = next((key for key in candidate_keys if key[2] == variant_id), candidate_keys[0])
        used_b.add(key_b)
        left = trial_summary([run["task_outcome"] for run in values_a])
        right = trial_summary([run["task_outcome"] for run in groups_b[key_b]])
        scope = variable_scope(values_a[0]["fingerprint"], groups_b[key_b][0]["fingerprint"])
        rows.append(
            {
                "case_id": case_id,
                "case_revision": revision,
                "variant_a": variant_id,
                "variant_b": key_b[2],
                "baseline": left,
                "candidate": right,
                "label": _transition(left, right),
                "scope": {
                    "kind": _expected_scope(scope),
                    "same": scope["same"],
                    "changed": scope["changed"],
                    "unknown": scope["unknown"],
                },
                "confidence": comparison_confidence(scope),
            }
        )
    mismatches: list[dict[str, Any]] = []
    ids_a = defaultdict(set)
    ids_b = defaultdict(set)
    for case_id, revision, _ in groups_a:
        ids_a[case_id].add(revision)
    for case_id, revision, _ in groups_b:
        ids_b[case_id].add(revision)
    for case_id in sorted(set(ids_a) & set(ids_b)):
        if ids_a[case_id] != ids_b[case_id]:
            mismatches.append(
                {
                    "case_id": case_id,
                    "experiment_a_revisions": sorted(ids_a[case_id]),
                    "experiment_b_revisions": sorted(ids_b[case_id]),
                    "strict_pairing": False,
                }
            )
    return {
        "experiment_a": experiment_a,
        "experiment_b": experiment_b,
        "matrix_a": matrix_report(runs_a),
        "matrix_b": matrix_report(runs_b),
        "differential_cases": rows,
        "case_revision_mismatches": mismatches,
        "note": "所有标签都是确定性的描述；系统不会推断因果概率。",
    }
