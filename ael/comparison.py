from __future__ import annotations

import json
from collections import defaultdict
import difflib
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
        path = Path(run["run_dir"]) / "telemetry" / "otel" / "events.jsonl"
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
    otel_events = [event for event in result if event.get("source") == "otel"]
    if not otel_events:
        return result

    def timestamp(event: dict[str, Any]) -> int:
        try:
            return int(str(event.get("timestamp") or "0"))
        except ValueError:
            return 0

    otel_events.sort(key=timestamp)
    native_structure = [
        event
        for event in result
        if event.get("source") != "otel"
        and event.get("kind") in {"command", "file_change", "verification", "final"}
    ]
    # Native tool events are usually duplicated by Claude's OTel tool logs;
    # keep the OTel sequence for the action timeline and native completion as
    # the local driver boundary.
    return [*otel_events, *native_structure]


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


def _action_step(event: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(event.get("kind") or "unknown")
    source = str(event.get("source") or "native")
    # OTel metrics, API requests, assistant messages, and resource lifecycle
    # records are evidence, but they are not Coding Agent action groups.
    if source == "otel" and kind not in {"tool_call", "tool_result", "command", "file_change", "verification"}:
        return None
    name = str(event.get("name") or "")
    summary = str(event.get("summary") or name or "").strip()
    text = f"{name} {summary}".lower()
    if kind == "file_change":
        group, label = "MUTATE", "MUTATE"
    elif kind == "verification":
        group, label = "VERIFY", "VERIFY"
    elif kind == "command":
        if any(token in text for token in ("pytest", "test", "verify", "check")):
            group = "VERIFY"
            label = "VERIFY targeted" if any(token in text for token in ("targeted", "-k ", "focused")) else "VERIFY full"
        elif any(token in text for token in ("cat ", "sed ", "head ", "tail ", "rg ", "grep ", "find ", "ls ", "pwd")):
            group, label = "READ", "READ / SEARCH"
        else:
            group, label = "TOOL", "TOOL"
    elif kind in {"tool_call", "tool_result"}:
        group, label = "TOOL", f"TOOL {kind.replace('_', ' ').upper()}"
    elif kind == "final":
        group, label = "COMPLETE", "COMPLETE"
    elif kind == "message" and name in {"api_request", "tool_decision", "assistant_response", "user_prompt"}:
        group, label = "TOOL", "MODEL CALL" if name == "api_request" else "TOOL"
    else:
        return None
    return {
        "group": group,
        "label": label,
        "detail": summary or label,
        "source": source,
        "kind": kind,
    }


def _action_steps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for event in events:
        step = _action_step(event)
        if not step:
            continue
        if steps and steps[-1]["group"] == step["group"] and steps[-1]["label"] == step["label"] and step["group"] in {"READ", "TOOL"}:
            continue
        steps.append(step)
    return steps


def _lcs_pairs(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[tuple[int, int]]:
    left_groups = [step["group"] for step in left]
    right_groups = [step["group"] for step in right]
    table = [[0] * (len(right_groups) + 1) for _ in range(len(left_groups) + 1)]
    for i in range(len(left_groups) - 1, -1, -1):
        for j in range(len(right_groups) - 1, -1, -1):
            if left_groups[i] == right_groups[j]:
                table[i][j] = table[i + 1][j + 1] + 1
            else:
                table[i][j] = max(table[i + 1][j], table[i][j + 1])
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < len(left_groups) and j < len(right_groups):
        if left_groups[i] == right_groups[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _meaningful_divergence_from_steps(
    candidate_steps: list[dict[str, Any]],
    reference_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    meaningful = {"MUTATE", "VERIFY", "COMPLETE"}
    candidate = [step for step in candidate_steps if step["group"] in meaningful]
    reference = [step for step in reference_steps if step["group"] in meaningful]
    if not candidate or not reference:
        return {"status": "NO_CLEAR_DIVERGENCE"}
    pairs = _lcs_pairs(candidate, reference)
    previous_left = previous_right = -1
    for left_index, right_index in pairs:
        if left_index > previous_left + 1 or right_index > previous_right + 1:
            return {
                "status": "DIVERGENCE",
                "reason": "unmatched strong action group",
                "candidate": candidate[previous_left + 1] if left_index > previous_left + 1 else None,
                "reference": reference[previous_right + 1] if right_index > previous_right + 1 else None,
            }
        if candidate[left_index]["label"] != reference[right_index]["label"]:
            return {
                "status": "DIVERGENCE",
                "reason": "same action group has different observed outcome",
                "candidate": candidate[left_index],
                "reference": reference[right_index],
            }
        previous_left, previous_right = left_index, right_index
    if previous_left + 1 < len(candidate) or previous_right + 1 < len(reference):
        return {
            "status": "DIVERGENCE",
            "reason": "unmatched strong action group",
            "candidate": candidate[previous_left + 1] if previous_left + 1 < len(candidate) else None,
            "reference": reference[previous_right + 1] if previous_right + 1 < len(reference) else None,
        }
    return {"status": "NO_CLEAR_DIVERGENCE"}


def first_meaningful_divergence(
    candidate_events: list[dict[str, Any]],
    reference_events: list[dict[str, Any]],
) -> dict[str, Any]:
    return _meaningful_divergence_from_steps(_action_steps(candidate_events), _action_steps(reference_events))


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
    changed_files = (result.get("changes") or {}).get("changed_files") or []
    result["meaningful_changed_files"] = [
        path
        for path in changed_files
        if not path.startswith("__pycache__/")
        and not path.startswith(".pytest_cache/")
        and not path.endswith((".pyc", ".pyo"))
    ]
    return result


def _artifact_reference_diff(
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> str:
    left = artifact_diff(None, candidate).get("diff", "")
    right = artifact_diff(None, reference).get("diff", "")
    if left == right:
        return ""
    return "".join(
        difflib.unified_diff(
            left.splitlines(keepends=True),
            right.splitlines(keepends=True),
            fromfile="candidate patch",
            tofile="PASS reference patch",
        )
    )


def _verifier_result(run: dict[str, Any]) -> dict[str, Any]:
    if isinstance(run.get("verifier"), dict):
        return run["verifier"]
    path = Path(run["run_dir"]) / "verifier" / "result.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _timeline(repository: Repository, run: dict[str, Any]) -> list[dict[str, Any]]:
    steps = _action_steps(_events(repository, run))
    artifact = artifact_diff(repository, run)
    changed_files = artifact.get("meaningful_changed_files") or []
    if changed_files and not any(step["group"] == "MUTATE" for step in steps):
        mutation = {
            "group": "MUTATE",
            "label": "MUTATE",
            "detail": ", ".join(changed_files),
            "source": "workspace",
            "kind": "file_change",
        }
        insert_at = next((index for index, step in enumerate(steps) if step["group"] == "COMPLETE"), len(steps))
        steps.insert(insert_at, mutation)
    verifier = _verifier_result(run)
    if not any(step["group"] == "VERIFY" for step in steps) and verifier.get("outcome") in {"PASS", "FAIL"}:
        steps.append(
            {
                "group": "VERIFY",
                "label": f"FULL VERIFY {verifier['outcome']}",
                "detail": "verifier result",
                "source": "verifier",
                "kind": "verification",
            }
        )
    return steps


def _run_summary(repository: Repository, run: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    metadata_path = Path(run["run_dir"]) / "metadata.json"
    if metadata_path.exists():
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                metadata = value
        except json.JSONDecodeError:
            pass
    telemetry: dict[str, Any] = {}
    telemetry_path = Path(run["run_dir"]) / "telemetry" / "summary.json"
    if telemetry_path.exists():
        try:
            value = json.loads(telemetry_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                telemetry = value
        except json.JSONDecodeError:
            pass
    native_usage = telemetry.get("native_usage") if isinstance(telemetry.get("native_usage"), dict) else telemetry
    otel = telemetry.get("otel") if isinstance(telemetry.get("otel"), dict) else {}
    artifact = artifact_diff(repository, run)
    timeline = _timeline(repository, run)
    return {
        "status": run.get("run_status"),
        "outcome": run.get("task_outcome"),
        "tests": (_verifier_result(run).get("outcome") or "UNKNOWN"),
        "duration_seconds": metadata.get("duration_seconds"),
        "tokens": {
            "input": native_usage.get("input_tokens") if isinstance(native_usage, dict) else None,
            "output": native_usage.get("output_tokens") if isinstance(native_usage, dict) else None,
            "otel_input": otel.get("input_tokens"),
            "otel_output": otel.get("output_tokens"),
        },
        "tool_calls": sum(1 for step in timeline if step["group"] == "TOOL"),
        "changed_files": artifact.get("meaningful_changed_files", []),
        "evidence_coverage": run.get("evidence_coverage", {}),
        "otel": otel,
    }


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
        "candidate_summary": _run_summary(repository, candidate),
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
    candidate_timeline = _timeline(repository, candidate)
    reference_timeline = _timeline(repository, reference)
    divergence = _meaningful_divergence_from_steps(candidate_timeline, reference_timeline)
    if candidate.get("task_outcome") != reference.get("task_outcome"):
        candidate_outcome = candidate.get("task_outcome") or "UNKNOWN"
        reference_outcome = reference.get("task_outcome") or "UNKNOWN"
        divergence = {
            "status": "DIVERGENCE",
            "reason": "verifier outcome differs",
            "candidate": {
                "group": "VERIFY",
                "label": f"FULL VERIFY {candidate_outcome}",
                "detail": "verifier task truth",
                "source": "verifier",
                "kind": "verification",
            },
            "reference": {
                "group": "VERIFY",
                "label": f"FULL VERIFY {reference_outcome}",
                "detail": "verifier task truth",
                "source": "verifier",
                "kind": "verification",
            },
        }
    candidate_artifact = artifact_diff(repository, candidate)
    candidate_artifact["candidate_reference_diff"] = _artifact_reference_diff(candidate, reference)
    result.update(
        {
            "matched_reference": {
                "run_id": reference["id"],
                "outcome": reference["task_outcome"],
                "score": match["score"],
            },
            "reference_summary": _run_summary(repository, reference),
            "artifact_diff": candidate_artifact,
            "variable_scope": match["scope"],
            "timeline_diff": {
                "candidate": candidate_timeline,
                "reference": reference_timeline,
            },
            "first_meaningful_divergence": divergence,
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
