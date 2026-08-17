from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .comparison import compare_run_details
from .persistence import Repository
from .redaction import redact


def _display_values(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(item) for item in value) or "无"
    return str(value)


def build_diagnosis_packet(
    repository: Repository,
    run_id: str,
    *,
    contrast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details = contrast if contrast is not None else compare_run_details(repository, run_id)
    if not details:
        return {}
    candidate = details.get("candidate", {})
    candidate_summary = details.get("candidate_summary") or {}
    reference_summary = details.get("reference_summary") or {}
    status_labels = {"PENDING": "等待运行", "RUNNING": "运行中", "COMPLETED": "已完成", "ERROR": "运行错误"}
    observed = [
        f"候选进程状态为 {status_labels.get(candidate.get('status'), candidate.get('status', '未知'))}。",
        f"Verifier 任务真值为 {candidate.get('outcome', 'UNKNOWN')}。",
    ]
    evidence: list[str] = []
    counter_evidence: list[str] = []
    unknowns: list[str] = []
    artifact = details.get("artifact_diff", {})
    changes = artifact.get("changes", {})
    changed_files = details.get("artifact_diff", {}).get("meaningful_changed_files", [])
    if changed_files:
        evidence.append(f"候选 workspace 观察到变化文件：{_display_values(changed_files)}。")
    else:
        evidence.append("候选 workspace 没有观察到文件变化。")
    if details.get("matched_reference"):
        reference_id = details["matched_reference"]["run_id"]
        evidence.append(f"用户明确选择的 Reference：{reference_id}。")
        scope = details.get("variable_scope") or {}
        if scope.get("changed"):
            evidence.append(f"已记录的变化变量：{_display_values(scope['changed'])}。")
        if scope.get("same"):
            counter_evidence.append(f"已记录的固定变量：{_display_values(scope['same'])}。")
        if reference_summary:
            evidence.append(
                f"参考 verifier={reference_summary.get('tests', 'UNKNOWN')}；"
                f"变更文件={_display_values(reference_summary.get('changed_files', []))}。"
            )
            if candidate.get("outcome") != reference_summary.get("outcome"):
                evidence.append(
                    f"候选 verifier={candidate.get('outcome', 'UNKNOWN')}；"
                    f"参考 verifier={reference_summary.get('outcome', 'UNKNOWN')}。"
                )
    else:
        unknowns.append("没有用户明确选择且 revision 相同的 Reference。")
    divergence = details.get("first_meaningful_divergence", {})
    if divergence.get("status") == "DIVERGENCE":
        left = divergence.get("candidate") or {}
        right = divergence.get("reference") or {}
        evidence.append(
            f"首个有意义的分歧：候选 {left.get('label', '—')} / "
            f"参考 {right.get('label', '—')}。"
        )
        if divergence.get("reason") == "verifier outcome differs":
            observed.append(
                f"候选观察到 {left.get('label', '完整验证未知')}；"
                f"参考观察到 {right.get('label', '完整验证未知')}。"
            )
        else:
            observed.append(
                f"候选观察到行为={left.get('detail', '没有')}；"
                f"参考观察到行为={right.get('detail', '没有')}。"
            )
    elif divergence.get("status") == "VERIFIER_BOUNDARY":
        left = divergence.get("candidate") or {}
        right = divergence.get("reference") or {}
        observed.append(
            f"Verifier 阶段结果不同：候选 {left.get('label', '未知')}；"
            f"参考 {right.get('label', '未知')}。"
        )
        evidence.append(
            f"Verifier 阶段证据：候选={left.get('detail', '未知')}；"
            f"参考={right.get('detail', '未知')}。"
        )
        unknowns.append("可观察行为组没有找到可靠分歧；当前只能定位到 verifier 边界。")
    else:
        unknowns.append("行为组对齐没有找到可靠的有意义分歧。")
    coverage = details.get("evidence_coverage", {})
    coverage_labels = {
        "Outcome": "结果",
        "Workspace": "工作区",
        "Tool timeline": "工具时间线",
        "Token usage": "Token 用量",
        "Model calls": "Model 调用",
        "Compaction event": "Compaction 事件",
        "Subagent lifecycle": "Subagent 生命周期",
    }
    for name, value in coverage.items():
        if value in {"?", "✗"}:
            unknowns.append(f"{coverage_labels.get(name, name)} 的证据覆盖为 {value}。")
    otel = candidate_summary.get("otel") or {}
    if otel.get("events"):
        evidence.append(f"Claude/OTel 关联事件：{otel['events']}，来源={otel.get('source', 'unknown')}。")
    else:
        unknowns.append("候选没有已关联的 OTel 记录。")
    if divergence.get("status") == "VERIFIER_BOUNDARY" or divergence.get("reason") == "verifier outcome differs":
        hypothesis_text = (
            "候选与明确 Reference 的任务真值不同；当前可确认的是 verifier 边界差异，"
            "但没有可靠的行为组分歧。产物 / verifier 输出可用于提出下一次实验，"
            "不能单独证明因果。"
        )
    elif divergence.get("status") == "DIVERGENCE":
        hypothesis_text = (
            "候选与明确 Reference 在可观察行为组上不同；这与完成 / 修复 "
            "路径差异一致，但当前证据不能证明单一因果。"
        )
    else:
        hypothesis_text = "当前只能确认 verifier 结果不同，不能从现有 trajectory 证据定位行为原因。"
    hypotheses = [
        {
            "statement": hypothesis_text,
            "evidence": evidence,
            "counter_evidence": counter_evidence,
            "certainty": "基于证据的假设；不输出因果概率",
        }
    ]
    changed = ((details.get("variable_scope") or {}).get("changed") or [])
    proposed_variable = changed[0] if len(changed) == 1 else "运行模式（run_mode）"
    return {
        "observed": observed,
        "hypotheses": hypotheses,
        "evidence": evidence,
        "counter_evidence": counter_evidence,
        "unknowns": sorted(set(unknowns)),
        "suggested_improvement": "保持 Agent、Case 版本和观测配置固定，编辑一个真实会改变 driver 行为的独立变量后重跑。",
        "best_next_experiment": {
            "status": "DRAFT",
            "requires_user_confirmation": True,
            "proposed_independent_variable": proposed_variable,
            "same_case_revision": bool(candidate.get("case_revision")),
            "trials": 2,
        },
        "source_run_id": run_id,
        "model_assisted": False,
    }


def _model_endpoint() -> tuple[str, str, str] | None:
    base_url = os.environ.get("AEL_DIAGNOSIS_BASE_URL")
    api_key = os.environ.get("AEL_DIAGNOSIS_API_KEY")
    model = os.environ.get("AEL_DIAGNOSIS_MODEL")
    if not base_url or not api_key or not model:
        return None
    return base_url.rstrip("/"), api_key, model


def _model_diagnosis(packet: dict[str, Any]) -> dict[str, Any] | None:
    config = _model_endpoint()
    if not config:
        return None
    base_url, api_key, model = config
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return JSON with exactly observed, hypotheses, evidence, counter_evidence, "
                    "unknowns, suggested_improvement, and best_next_experiment. Never claim causal certainty."
                ),
            },
            {"role": "user", "content": json.dumps(redact(packet), sort_keys=True)},
        ],
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        value = json.loads(content) if isinstance(content, str) else content
    except (OSError, urllib.error.URLError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    required = {
        "observed",
        "hypotheses",
        "evidence",
        "counter_evidence",
        "unknowns",
        "suggested_improvement",
        "best_next_experiment",
    }
    if not required <= value.keys():
        return None
    value["model_assisted"] = True
    value["source_run_id"] = packet.get("source_run_id")
    return redact(value)


def diagnose_run(
    repository: Repository,
    run_id: str,
    *,
    contrast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    packet = build_diagnosis_packet(repository, run_id, contrast=contrast)
    model_packet = _model_diagnosis(packet)
    return model_packet or packet
