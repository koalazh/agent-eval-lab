from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .cases import ExperimentSpec, SuiteSpec
from .comparison import compare_run_details
from .models import AgentVariant, RunMode
from .persistence import Repository
from .redaction import redact


def _display_values(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(item) for item in value) or "无"
    return str(value)


def build_diagnosis_packet(repository: Repository, run_id: str) -> dict[str, Any]:
    details = compare_run_details(repository, run_id)
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


def diagnose_run(repository: Repository, run_id: str) -> dict[str, Any]:
    packet = build_diagnosis_packet(repository, run_id)
    model_packet = _model_diagnosis(packet)
    return model_packet or packet


def _variant_from_definition(raw: dict[str, Any]) -> AgentVariant:
    return AgentVariant.from_dict(raw)


def _toggle_run_mode(mode: RunMode) -> RunMode:
    return RunMode.CONTROLLED if mode == RunMode.NATIVE else RunMode.NATIVE


def create_follow_up_experiment(
    repository: Repository,
    run_id: str,
    *,
    independent_variable: str = "run_mode",
    candidate_agent_id: str | None = None,
    trials: int | None = None,
    max_concurrency: int | None = None,
    save: bool = True,
) -> ExperimentSpec:
    run = repository.get_run(run_id)
    if not run:
        raise ValueError(f"未找到运行记录：{run_id}")
    definition = repository.read_experiment_definition(run["experiment_id"])
    if not definition:
        raise ValueError("源实验 definition 不可用，无法创建可运行的后续实验")
    suite_id = str((definition.get("suite") or {}).get("id") or "follow-up")
    cases = tuple(repository.suite_cases(suite_id))
    if not cases:
        raise ValueError("源 Case revision 不可用，无法创建相同 revision 的后续实验")
    if not any(case.id == run["case_id"] and case.revision == run["case_revision"] for case in cases):
        raise ValueError("源 Run 的 Case revision 不在原实验 Suite 中")
    suite = SuiteSpec(suite_id, str((definition.get("suite") or {}).get("kind") or "coding"), cases)
    source_raw = next(
        (raw for raw in definition.get("variants", []) if raw.get("id") == run["variant_id"]),
        None,
    )
    if not source_raw:
        raise ValueError("源 Variant 不在原 experiment definition 中")
    source_variant = _variant_from_definition(source_raw)
    if independent_variable not in {"prompt_intervention", "run_mode", "agent"}:
        raise ValueError("当前 Follow-up Builder 只支持 prompt intervention、run_mode 或 Agent")
    if independent_variable == "agent":
        if not candidate_agent_id:
            raise ValueError("Agent 消融必须选择 candidate Agent")
        candidate_agent = candidate_agent_id
    else:
        candidate_agent = source_variant.agent_id
    baseline_config = dict(source_variant.harness_config)
    candidate_config = dict(source_variant.harness_config)
    baseline_mode = source_variant.run_mode
    candidate_mode = source_variant.run_mode
    if independent_variable == "prompt_intervention":
        baseline_config["prompt_intervention"] = False
        candidate_config["prompt_intervention"] = True
    elif independent_variable == "run_mode":
        candidate_mode = _toggle_run_mode(source_variant.run_mode)
    baseline_id = f"baseline-{run_id[:8]}"
    candidate_id = f"candidate-{run_id[:8]}"
    baseline = AgentVariant(
        id=baseline_id,
        name=f"Baseline · {source_variant.name or source_variant.id}",
        agent_id=source_variant.agent_id,
        executable=source_variant.executable,
        subject_revision=source_variant.subject_revision,
        agent_version=source_variant.agent_version,
        model=source_variant.model,
        provider=source_variant.provider,
        model_config=source_variant.model_config,
        harness_config=baseline_config,
        run_mode=baseline_mode,
        observation_profile=source_variant.observation_profile,
    )
    candidate = AgentVariant(
        id=candidate_id,
        name=f"Candidate · {source_variant.name or source_variant.id}",
        agent_id=candidate_agent,
        executable=source_variant.executable,
        subject_revision=source_variant.subject_revision,
        agent_version=source_variant.agent_version,
        model=source_variant.model if candidate_agent == source_variant.agent_id else "default",
        provider=source_variant.provider if candidate_agent == source_variant.agent_id else "default",
        model_config=source_variant.model_config if candidate_agent == source_variant.agent_id else {},
        harness_config=candidate_config,
        run_mode=candidate_mode,
        observation_profile=source_variant.observation_profile,
    )
    experiment_id = f"follow-up-{run['experiment_id']}-{run_id[:8]}-{uuid.uuid4().hex[:6]}"
    follow_up = ExperimentSpec(
        id=experiment_id,
        suite=suite,
        variants=(baseline, candidate),
        trials=max(1, trials if trials is not None else int(definition.get("trials") or 1)),
        max_concurrency=max(1, max_concurrency if max_concurrency is not None else int(definition.get("max_concurrency") or 1)),
        source_path=None,
        metadata={
            "created_from": "follow_up_builder",
            "follow_up_of_run": run_id,
            "independent_variable": independent_variable,
            "baseline_variant_id": baseline_id,
            "candidate_variant_id": candidate_id,
            "baseline_value": baseline_mode.value if independent_variable == "run_mode" else baseline_config.get(independent_variable, False),
            "candidate_value": candidate_mode.value if independent_variable == "run_mode" else candidate_config.get(independent_variable, True),
            "source_variant_id": source_variant.id,
        },
    )
    if save:
        repository.save_experiment(follow_up, status="DRAFT", follow_up_of=run_id)
    return follow_up
