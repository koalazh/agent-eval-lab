from __future__ import annotations

import json
from collections import defaultdict
import difflib
from pathlib import Path
from typing import Any

from .hashing import UNKNOWN, canonical_json
from .persistence import Repository
from .reports import matrix_report, trial_summary
from .trace_view import build_metric_snapshot, build_trace_view, build_verifier_phases


_FINGERPRINT_FIELDS = (
    ("agent_id", "Agent"),
    ("agent_version", "Agent 版本"),
    ("driver", "Driver"),
    ("driver_version", "Driver 版本"),
    ("model", "Model"),
    ("provider", "Provider"),
    ("model_config", "Model 配置"),
    ("harness_config_hash", "Harness 配置"),
    ("run_mode", "运行模式"),
    ("observation_profile", "观测配置"),
    ("runtime", "Runtime"),
    ("case_revision", "Case revision"),
    ("prompt_hash", "Prompt"),
    ("fixture_hash", "Fixture"),
    ("ael_version", "AEL 版本"),
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
        return "Model 差异"
    if changed and changed <= {"Agent", "Driver", "Agent 版本", "Driver 版本"}:
        return "Harness 差异"
    if changed == {"Harness 配置"}:
        return "Feature 差异"
    if changed == {"Agent 版本", "Driver 版本"}:
        return "版本差异"
    return "描述性差异"


def comparison_confidence(scope: dict[str, Any]) -> str:
    if scope["unknown"]:
        return "PARTIAL"
    changed = set(scope["changed"])
    if not changed:
        return "CONTROLLED"
    if changed in (
        {"Model"},
        {"Harness 配置"},
        {"Agent", "Driver"},
        {"Agent 版本", "Driver 版本"},
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
                "reason": (
                    "verifier outcome differs"
                    if candidate[left_index].get("source") == reference[right_index].get("source") == "verifier"
                    else "same action group has different observed outcome"
                ),
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def _timeline(repository: Repository, run: dict[str, Any]) -> list[dict[str, Any]]:
    steps = _action_steps(_events(repository, run))
    artifact = artifact_diff(repository, run)
    changed_files = artifact.get("meaningful_changed_files") or []
    if changed_files and not any(step["group"] == "MUTATE" for step in steps):
        mutation = {
            "group": "MUTATE",
            "label": "代码变更",
            "detail": ", ".join(changed_files),
            "source": "workspace",
            "kind": "file_change",
        }
        insert_at = next((index for index, step in enumerate(steps) if step["group"] == "COMPLETE"), len(steps))
        steps.insert(insert_at, mutation)
    verifier = _verifier_result(run)
    phases = build_verifier_phases(verifier)
    for phase in phases:
        steps.append(
            {
                "group": "VERIFY",
                "label": f"{phase['label']} {phase['status']}",
                "detail": phase["detail"],
                "source": "verifier",
                "kind": "verification",
            }
        )
    if not phases and not any(step["group"] == "VERIFY" for step in steps) and verifier.get("outcome") in {"PASS", "FAIL"}:
        steps.append(
            {
                "group": "VERIFY",
                "label": f"完整验证 {verifier['outcome']}",
                "detail": "verifier 结果",
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
    otel = telemetry.get("otel") if isinstance(telemetry.get("otel"), dict) else {}
    artifact = artifact_diff(repository, run)
    evidence_root = Path(run["run_dir"])
    otel_events = _read_jsonl(evidence_root / "telemetry" / "otel" / "events.jsonl")
    native_events = _read_jsonl(evidence_root / "native" / "events.jsonl")
    trace_view = build_trace_view(otel_events, native_events)
    metrics = build_metric_snapshot(telemetry, otel_events, native_events, trace_view)
    fingerprint = run.get("fingerprint") if isinstance(run.get("fingerprint"), dict) else {}
    return {
        "status": run.get("run_status"),
        "outcome": run.get("task_outcome"),
        "tests": (_verifier_result(run).get("outcome") or "UNKNOWN"),
        "duration_seconds": metadata.get("duration_seconds"),
        "tokens": {
            "input": metrics["input_tokens"],
            "output": metrics["output_tokens"],
            "otel_input": otel.get("input_tokens"),
            "otel_output": otel.get("output_tokens"),
        },
        "tool_calls": metrics["tool_calls"],
        "changed_files": artifact.get("meaningful_changed_files", []),
        "evidence_coverage": run.get("evidence_coverage", {}),
        "otel": otel,
        "metrics": metrics,
        "metric_labels": {
            "total_tokens": _number_label(metrics.get("total_tokens")),
            "tool_calls": _number_label(metrics.get("tool_calls")),
            "model_calls": _number_label(metrics.get("model_calls")),
            "cost_usd": _cost_label(metrics.get("cost_usd")),
        },
        "identity": {
            "agent": fingerprint.get("agent_id"),
            "agent_version": fingerprint.get("agent_version"),
            "model": fingerprint.get("model"),
            "provider": fingerprint.get("provider"),
            "variant_id": run.get("variant_id"),
        },
    }


def _mean(values: list[Any]) -> float | None:
    numbers: list[float] = []
    for value in values:
        try:
            numbers.append(float(value))
        except (TypeError, ValueError):
            continue
    return sum(numbers) / len(numbers) if numbers else None


def _number_label(value: Any, fallback: str = "未知", decimals: int = 0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if decimals == 0:
        return f"{int(round(number)):,}"
    return f"{number:,.{decimals}f}"


def _seconds_label(value: Any) -> str:
    try:
        return f"{float(value):.2f} 秒"
    except (TypeError, ValueError):
        return "未知"


def _cost_label(value: Any) -> str:
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "未知"


def _configured_label(value: Any) -> str:
    value = str(value or "default")
    return "默认配置" if value.lower() in {"default", "unknown"} else value


def _identity_label(value: Any) -> str:
    if value is None or str(value).upper() == UNKNOWN:
        return UNKNOWN
    return _configured_label(value)


def _aggregate_metric(summaries: list[dict[str, Any]], key: str) -> float | None:
    return _mean([(summary.get("metrics") or {}).get(key) for summary in summaries])


def _evidence_label(metrics: dict[str, Any]) -> str:
    if metrics.get("otel_events") is None:
        native_events = metrics.get("native_events")
        return f"native {_number_label(native_events, decimals=1)} 个事件" if native_events is not None else "未知"
    return (
        f"logs {_number_label(metrics.get('otel_logs'))} · "
        f"metrics {_number_label(metrics.get('otel_metrics'))} · "
        f"span {_number_label(metrics.get('otel_spans'))}"
    )


def _comparison_row(
    repository: Repository,
    case_id: str,
    variant_id: str,
    runs: list[dict[str, Any]],
    variant: dict[str, Any],
    *,
    differential: bool,
    target_run_id: str | None,
) -> dict[str, Any]:
    summaries = [_run_summary(repository, run) for run in runs]
    outcomes = [run.get("task_outcome", run.get("outcome", "UNKNOWN")) for run in runs]
    result = trial_summary(outcomes)
    metric_values = {
        key: _aggregate_metric(summaries, key)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "total_tokens",
            "cost_usd",
            "otel_duration_ms",
            "model_calls",
            "tool_calls",
            "tool_errors",
            "otel_events",
            "otel_logs",
            "otel_metrics",
            "otel_spans",
            "native_events",
        )
    }
    changed_files = _mean([len(summary.get("changed_files") or []) for summary in summaries])
    actual_models = sorted(
        {
            model
            for summary in summaries
            for model in (summary.get("metrics") or {}).get("models", [])
        }
    )
    configured_model = _configured_label(variant.get("model"))
    model_label = ", ".join(actual_models) if actual_models else ("未知" if configured_model == "默认配置" else configured_model)
    configured_provider = _configured_label(variant.get("provider"))
    provider_label = "未知" if configured_provider == "默认配置" else configured_provider
    model_calls = metric_values["model_calls"]
    tool_calls = metric_values["tool_calls"]
    tool_errors = metric_values["tool_errors"]
    return {
        "case_id": case_id,
        "variant_id": variant_id,
        "variant_label": _variant_label_for_report(variant),
        "agent": str(variant.get("agent_id") or variant_id),
        "agent_type": str(variant.get("agent_id") or variant_id),
        "model": model_label,
        "configured_model": configured_model,
        "provider": provider_label,
        "result": result,
        "classification": result["classification"],
        "differential": differential,
        "target_run_id": target_run_id,
        "trial_label": result["display"],
        "duration_label": _seconds_label(_mean([summary.get("duration_seconds") for summary in summaries])),
        "otel_duration_label": _seconds_label(
            metric_values["otel_duration_ms"] / 1000 if metric_values["otel_duration_ms"] is not None else None
        ),
        "input_tokens_label": _number_label(metric_values["input_tokens"]),
        "output_tokens_label": _number_label(metric_values["output_tokens"]),
        "cache_read_label": _number_label(metric_values["cache_read_tokens"]),
        "cache_creation_label": _number_label(metric_values["cache_creation_tokens"]),
        "total_tokens_label": _number_label(metric_values["total_tokens"]),
        "cost_label": _cost_label(metric_values["cost_usd"]),
        "model_calls_label": _number_label(model_calls),
        "tool_calls_label": _number_label(tool_calls),
        "tool_errors_label": _number_label(tool_errors),
        "changed_files_label": _number_label(changed_files, decimals=1),
        "evidence_label": _evidence_label(metric_values),
        "evidence_note": "每次 trial 平均；没有 OTel 时显示 native 事件数，无法观测时保持未知",
        "metrics": metric_values,
        "run_count": len(runs),
    }


def _variant_label_for_report(variant: dict[str, Any]) -> str:
    agent = variant.get("agent_id") or variant.get("id") or "Variant"
    label = f"{agent} / {_configured_label(variant.get('model'))}"
    config = variant.get("harness_config") or {}
    if config.get("verification_gate") is True:
        label += " · verification_gate=on"
    elif config.get("verification_gate") is False:
        label += " · verification_gate=off"
    return label


_PIVOT_VALUE_LABELS = {
    "STABLE_PASS": "稳定通过",
    "STABLE_FAIL": "稳定失败",
    "FLAKY": "不稳定",
    "ERROR": "运行错误",
    "UNKNOWN": "结果不完整",
}

_PIVOT_TONES = {
    "STABLE_PASS": "pass",
    "STABLE_FAIL": "fail",
    "FLAKY": "flaky",
    "ERROR": "error",
    "UNKNOWN": "unknown",
}

_VARIANT_PIVOT_DEFINITIONS = (
    ("agent", "Agent 类型 / Variant", "identity"),
    ("model", "Model", "identity"),
    ("provider", "Provider", "identity"),
    ("scope", "覆盖范围", "result"),
    ("result", "Verifier 结果", "result"),
    ("duration", "平均 Run 时长", "performance"),
    ("otel_duration", "平均 OTel 时长", "performance"),
    ("input_tokens", "平均输入 tokens", "tokens"),
    ("output_tokens", "平均输出 tokens", "tokens"),
    ("cache_read", "平均缓存读取 tokens", "tokens"),
    ("cache_creation", "平均缓存创建 tokens", "tokens"),
    ("total_tokens", "平均总 tokens", "tokens"),
    ("cost", "平均成本", "cost"),
    ("tool_calls", "平均工具调用", "behavior"),
    ("model_calls", "平均模型轮数", "behavior"),
    ("tool_errors", "平均工具错误", "behavior"),
    ("changed_files", "平均变更文件", "behavior"),
    ("evidence", "OTel / native 证据", "evidence"),
)

_CASE_PIVOT_DEFINITIONS = (
    ("agent", "Agent 类型 / Variant", "identity"),
    ("model", "Model", "identity"),
    ("provider", "Provider", "identity"),
    ("result", "Verifier 结果", "result"),
    ("duration", "Run 时长", "performance"),
    ("otel_duration", "OTel 时长", "performance"),
    ("input_tokens", "输入 tokens", "tokens"),
    ("output_tokens", "输出 tokens", "tokens"),
    ("cache_read", "缓存读取 tokens", "tokens"),
    ("cache_creation", "缓存创建 tokens", "tokens"),
    ("total_tokens", "总 tokens", "tokens"),
    ("cost", "成本", "cost"),
    ("tool_calls", "工具调用", "behavior"),
    ("model_calls", "模型轮数", "behavior"),
    ("tool_errors", "工具错误", "behavior"),
    ("changed_files", "变更文件", "behavior"),
    ("evidence", "OTel / native 证据", "evidence"),
)


def _pivot_cell(row: dict[str, Any] | None, key: str) -> dict[str, Any]:
    if not row:
        return {"value": "未知", "detail": "没有对应 Run", "tone": "unknown"}
    classification = str(row.get("classification") or "UNKNOWN")
    # Only the verifier result carries a pass/fail meaning. Identity and cost
    # cells must stay visually neutral; otherwise a passing Run would make
    # every token/cost number look like a positive outcome.
    tone = _PIVOT_TONES.get(classification, "unknown") if key == "result" else "neutral"
    cell: dict[str, Any] = {"value": "未知", "detail": None, "tone": tone}
    if key == "agent":
        cell.update(value=row.get("agent_type") or "未知", detail=row.get("variant_label"))
    elif key == "model":
        cell.update(value=row.get("model") or "未知")
        if row.get("configured_model") != row.get("model"):
            cell["detail"] = f"配置：{row.get('configured_model') or '未知'}"
    elif key == "provider":
        cell.update(value=row.get("provider") or "未知")
    elif key == "scope":
        cell.update(value=f"{row.get('case_label', '1 个 Case')} / {row.get('run_count', 0)} 次")
    elif key == "result":
        cell.update(
            value=row.get("trial_label") or "未知",
            detail=_PIVOT_VALUE_LABELS.get(classification, classification),
        )
    elif key == "duration":
        cell.update(value=row.get("duration_label") or "未知")
    elif key == "otel_duration":
        cell.update(value=row.get("otel_duration_label") or "未知")
    elif key == "input_tokens":
        cell.update(value=row.get("input_tokens_label") or "未知")
    elif key == "output_tokens":
        cell.update(value=row.get("output_tokens_label") or "未知")
    elif key == "cache_read":
        cell.update(value=row.get("cache_read_label") or "未知")
    elif key == "cache_creation":
        cell.update(value=row.get("cache_creation_label") or "未知")
    elif key == "total_tokens":
        cell.update(value=row.get("total_tokens_label") or "未知")
    elif key == "cost":
        cell.update(value=row.get("cost_label") or "未知")
    elif key == "tool_calls":
        cell.update(value=row.get("tool_calls_label") or "未知")
    elif key == "model_calls":
        cell.update(value=row.get("model_calls_label") or "未知")
    elif key == "tool_errors":
        cell.update(value=row.get("tool_errors_label") or "未知")
    elif key == "changed_files":
        cell.update(value=row.get("changed_files_label") or "未知")
    elif key == "evidence":
        cell.update(value=row.get("evidence_label") or "未知", detail=row.get("evidence_note"))

    target_run_id = row.get("target_run_id")
    if target_run_id:
        cell["href"] = f"/runs/{target_run_id}{'/explorer' if row.get('result', {}).get('fails') else ''}"
    return cell


def _pivot_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row["variant_id"],
            "agent": row.get("agent_type") or row["variant_id"],
            "label": row.get("variant_label") or row["variant_id"],
        }
        for row in rows
    ]


def _pivot_metric_rows(
    rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    definitions: tuple[tuple[str, str, str], ...],
) -> list[dict[str, Any]]:
    by_variant = {row["variant_id"]: row for row in rows}
    return [
        {
            "key": key,
            "label": label,
            "group": group,
            "values": [_pivot_cell(by_variant.get(column["id"]), key) for column in columns],
        }
        for key, label, group in definitions
    ]


def _case_state(rows: list[dict[str, Any]]) -> str:
    if any(row.get("differential") for row in rows):
        return "differential"
    if any(row.get("classification") != "STABLE_PASS" for row in rows):
        return "problem"
    return "stable"


def _summary_metric_value(summary: dict[str, Any], run: dict[str, Any], key: str) -> Any:
    if key == "agent":
        return (summary.get("identity") or {}).get("agent") or UNKNOWN
    if key == "agent_version":
        return (summary.get("identity") or {}).get("agent_version") or UNKNOWN
    if key == "model":
        models = (summary.get("metrics") or {}).get("models") or []
        return ", ".join(models) if models else _identity_label((summary.get("identity") or {}).get("model"))
    if key == "provider":
        return _identity_label((summary.get("identity") or {}).get("provider"))
    if key == "outcome":
        return summary.get("tests") or run.get("task_outcome") or UNKNOWN
    if key == "duration_seconds":
        return summary.get("duration_seconds")
    if key == "changed_files":
        return len(summary.get("changed_files") or [])
    if key == "evidence":
        metrics = summary.get("metrics") or {}
        return _evidence_label(metrics)
    return (summary.get("metrics") or {}).get(key)


def _pair_display(value: Any, kind: str) -> str:
    if value is None or value == UNKNOWN:
        return "未知"
    if kind == "seconds":
        return _seconds_label(value)
    if kind == "milliseconds":
        return _seconds_label(float(value) / 1000)
    if kind == "cost":
        return _cost_label(value)
    if kind == "number":
        return _number_label(value)
    return str(value)


def _pair_delta(left: Any, right: Any, kind: str) -> str:
    if left is None or right is None or left == UNKNOWN or right == UNKNOWN:
        return "未知"
    if kind in {"seconds", "milliseconds", "cost", "number"}:
        try:
            delta = float(left) - float(right)
        except (TypeError, ValueError):
            return "未知"
        threshold = 0.00005 if kind == "cost" else 0.005
        if abs(delta) < threshold:
            return "相同"
        if kind == "seconds":
            return f"候选 {'+' if delta > 0 else ''}{delta:.2f} 秒"
        if kind == "milliseconds":
            return f"候选 {'+' if delta > 0 else ''}{delta / 1000:.2f} 秒"
        if kind == "cost":
            return f"候选 {'+' if delta > 0 else ''}${delta:.4f}"
        return f"候选 {'+' if delta > 0 else ''}{delta:.0f}"
    return "相同" if str(left) == str(right) else "不同"


def _metric_comparison_rows(
    candidate: dict[str, Any],
    candidate_summary: dict[str, Any],
    reference: dict[str, Any] | None,
    reference_summary: dict[str, Any] | None,
) -> list[dict[str, str]]:
    fields = (
        ("Agent 类型", "agent", "text"),
        ("Agent 版本", "agent_version", "text"),
        ("Model", "model", "text"),
        ("Provider", "provider", "text"),
        ("Verifier 结果", "outcome", "text"),
        ("端到端时长", "duration_seconds", "seconds"),
        ("OTel 活跃时长", "otel_duration_ms", "milliseconds"),
        ("模型轮数", "model_calls", "number"),
        ("工具调用", "tool_calls", "number"),
        ("工具错误", "tool_errors", "number"),
        ("输入 tokens", "input_tokens", "number"),
        ("输出 tokens", "output_tokens", "number"),
        ("缓存读取 tokens", "cache_read_tokens", "number"),
        ("缓存创建 tokens", "cache_creation_tokens", "number"),
        ("总 tokens", "total_tokens", "number"),
        ("成本", "cost_usd", "cost"),
        ("有效变更文件", "changed_files", "number"),
        ("OTel 证据", "evidence", "text"),
    )
    rows: list[dict[str, str]] = []
    for label, key, kind in fields:
        left = _summary_metric_value(candidate_summary, candidate, key)
        right = _summary_metric_value(reference_summary, reference, key) if reference and reference_summary else None
        rows.append(
            {
                "label": label,
                "candidate": _pair_display(left, kind),
                "reference": _pair_display(right, kind),
                "delta": _pair_delta(left, right, kind),
            }
        )
    return rows


def build_experiment_comparison(
    repository: Repository,
    runs: list[dict[str, Any]],
    variants: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the decision tables behind the Experiment detail page."""
    matrix = matrix_report(runs)
    variant_by_id = {str(variant.get("id")): variant for variant in variants}
    variant_order = [str(variant.get("id")) for variant in variants if variant.get("id")]
    variant_order.extend(item for item in matrix["variant_ids"] if item not in variant_order)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[(str(run["case_id"]), str(run["variant_id"]))].append(run)

    rows: list[dict[str, Any]] = []
    for (case_id, variant_id), group in sorted(
        grouped.items(), key=lambda item: (item[0][0], variant_order.index(item[0][1]) if item[0][1] in variant_order else 999)
    ):
        cell = next(
            (
                row["cells"].get(variant_id)
                for row in matrix["matrix_rows"]
                if row["case_id"] == case_id
            ),
            {},
        )
        rows.append(
            _comparison_row(
                repository,
                case_id,
                variant_id,
                group,
                variant_by_id.get(variant_id, {"id": variant_id}),
                differential=bool(cell.get("differential")),
                target_run_id=cell.get("target_run_id"),
            )
        )

    variant_rows: list[dict[str, Any]] = []
    for variant_id in variant_order:
        group = [run for run in runs if str(run.get("variant_id")) == variant_id]
        if not group:
            continue
        row = _comparison_row(
            repository,
            "全部 Case",
            variant_id,
            group,
            variant_by_id.get(variant_id, {"id": variant_id}),
            differential=False,
            target_run_id=None,
        )
        row["case_count"] = len({run.get("case_id") for run in group})
        row["case_label"] = f"{row['case_count']} 个 Case"
        variant_rows.append(row)

    variant_columns = _pivot_columns(variant_rows)
    variant_metric_rows = _pivot_metric_rows(variant_rows, variant_columns, _VARIANT_PIVOT_DEFINITIONS)
    rows_by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_case[row["case_id"]].append(row)
    case_comparisons: list[dict[str, Any]] = []
    case_filter_options: list[dict[str, str]] = []
    state_labels = {"differential": "差异", "problem": "失败 / 不稳定", "stable": "稳定通过"}
    for case_id in matrix["case_ids"]:
        case_rows = sorted(
            rows_by_case.get(case_id, []),
            key=lambda row: variant_order.index(row["variant_id"]) if row["variant_id"] in variant_order else 999,
        )
        if not case_rows:
            continue
        state = _case_state(case_rows)
        case_comparisons.append(
            {
                "case_id": case_id,
                "state": state,
                "state_label": state_labels[state],
                "metric_rows": _pivot_metric_rows(case_rows, variant_columns, _CASE_PIVOT_DEFINITIONS),
            }
        )
        case_filter_options.append({"id": case_id, "label": case_id, "state": state})

    return {
        "variant_rows": variant_rows,
        "case_variant_rows": rows,
        "cell_lookup": {f"{row['case_id']}::{row['variant_id']}": row for row in rows},
        "variant_columns": variant_columns,
        "variant_metric_rows": variant_metric_rows,
        "case_comparisons": case_comparisons,
        "case_filter_options": case_filter_options,
        "case_states": {option["id"]: option["state"] for option in case_filter_options},
        "notes": "数值默认按每次 trial 平均；成本只在 Agent/OTel 实际提供时显示；没有 OTel 时保留 native 事件覆盖；未知不等于 0。",
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
    result["metric_rows"] = _metric_comparison_rows(
        candidate,
        result["candidate_summary"],
        None,
        None,
    )
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
    if divergence.get("reason") == "verifier outcome differs":
        divergence["status"] = "VERIFIER_BOUNDARY"
    if candidate.get("task_outcome") != reference.get("task_outcome") and divergence.get("status") == "NO_CLEAR_DIVERGENCE":
        candidate_outcome = candidate.get("task_outcome") or "UNKNOWN"
        reference_outcome = reference.get("task_outcome") or "UNKNOWN"
        divergence = {
            "status": "VERIFIER_BOUNDARY",
            "reason": "verifier outcome differs",
            "candidate": {
                "group": "VERIFY",
                "label": f"完整验证 {candidate_outcome}",
                "detail": "verifier 任务真值",
                "source": "verifier",
                "kind": "verification",
            },
            "reference": {
                "group": "VERIFY",
                "label": f"完整验证 {reference_outcome}",
                "detail": "verifier 任务真值",
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
    result["metric_rows"] = _metric_comparison_rows(
        candidate,
        result["candidate_summary"],
        reference,
        result["reference_summary"],
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
