from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .cases import ExperimentSpec, SuiteSpec, load_experiment
from .comparison import compare_run_details
from .persistence import Repository
from .redaction import redact


def build_diagnosis_packet(repository: Repository, run_id: str) -> dict[str, Any]:
    details = compare_run_details(repository, run_id)
    if not details:
        return {}
    candidate = details.get("candidate", {})
    candidate_summary = details.get("candidate_summary") or {}
    reference_summary = details.get("reference_summary") or {}
    observed = [
        f"Candidate process 状态为 {candidate.get('status', 'UNKNOWN')}。",
        f"Verifier task truth 为 {candidate.get('outcome', 'UNKNOWN')}。",
    ]
    evidence: list[str] = []
    counter_evidence: list[str] = []
    unknowns: list[str] = []
    artifact = details.get("artifact_diff", {})
    changes = artifact.get("changes", {})
    changed_files = details.get("artifact_diff", {}).get("meaningful_changed_files", [])
    if changed_files:
        evidence.append(f"Candidate workspace 观察到变化文件：{changed_files}。")
    else:
        evidence.append("Candidate workspace 没有观察到文件变化。")
    if details.get("matched_reference"):
        reference_id = details["matched_reference"]["run_id"]
        evidence.append(f"Matched PASS reference：{reference_id}。")
        scope = details.get("variable_scope") or {}
        if scope.get("changed"):
            evidence.append(f"Recorded changed variables：{scope['changed']}。")
        if scope.get("same"):
            counter_evidence.append(f"Recorded fixed variables：{scope['same']}。")
        if reference_summary:
            evidence.append(
                f"Reference verifier={reference_summary.get('tests', 'UNKNOWN')}；"
                f"changed_files={reference_summary.get('changed_files', [])}。"
            )
            if candidate.get("outcome") != reference_summary.get("outcome"):
                evidence.append(
                    f"Candidate verifier={candidate.get('outcome', 'UNKNOWN')}；"
                    f"Reference verifier={reference_summary.get('outcome', 'UNKNOWN')}。"
                )
    else:
        unknowns.append("没有足够接近且 revision 相同的 PASS reference。")
    divergence = details.get("first_meaningful_divergence", {})
    if divergence.get("status") == "DIVERGENCE":
        left = divergence.get("candidate") or {}
        right = divergence.get("reference") or {}
        evidence.append(
            f"First meaningful divergence：Candidate {left.get('label', '—')} / "
            f"Reference {right.get('label', '—')}。"
        )
        if divergence.get("reason") == "verifier outcome differs":
            observed.append(
                f"Candidate observed {left.get('label', 'FULL VERIFY UNKNOWN')}；"
                f"reference observed {right.get('label', 'FULL VERIFY UNKNOWN')}。"
            )
        else:
            observed.append(
                f"Candidate observed action={left.get('detail', 'none')}；"
                f"reference observed action={right.get('detail', 'none')}。"
            )
    else:
        unknowns.append("Action-group alignment 没有找到可靠的有意义分歧。")
    coverage = details.get("evidence_coverage", {})
    for name, value in coverage.items():
        if value in {"?", "✗"}:
            unknowns.append(f"{name} 的 evidence coverage 为 {value}。")
    otel = candidate_summary.get("otel") or {}
    if otel.get("events"):
        evidence.append(f"Claude/OTel correlated events：{otel['events']}，source={otel.get('source', 'unknown')}。")
    else:
        unknowns.append("Candidate 没有 correlated OTel records。")
    if divergence.get("reason") == "verifier outcome differs":
        hypothesis_text = (
            "Candidate 与 PASS reference 的 task truth 不同；当前可确认的是 verifier boundary "
            "差异，artifact / action evidence 可用于提出下一次实验，但不能单独证明因果。"
        )
    elif divergence.get("status") == "DIVERGENCE":
        hypothesis_text = (
            "Candidate 与 PASS reference 在可观察 action group 上不同；这与 completion/repair "
            "路径差异一致，但当前证据不能证明单一因果。"
        )
    else:
        hypothesis_text = "当前只能确认 verifier 结果不同，不能从现有 trajectory 证据定位行为原因。"
    hypotheses = [
        {
            "statement": hypothesis_text,
            "evidence": evidence,
            "counter_evidence": counter_evidence,
            "certainty": "evidence-grounded hypothesis；不输出因果概率",
        }
    ]
    changed = ((details.get("variable_scope") or {}).get("changed") or [])
    proposed_variable = changed[0] if len(changed) == 1 else "Run mode"
    return {
        "observed": observed,
        "hypotheses": hypotheses,
        "evidence": evidence,
        "counter_evidence": counter_evidence,
        "unknowns": sorted(set(unknowns)),
        "suggested_improvement": "保持 Agent、Case revision 和 observation 固定，编辑一个真实会改变 driver 行为的 independent variable 后重跑。",
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


def create_follow_up_experiment(repository: Repository, run_id: str) -> ExperimentSpec:
    run = repository.get_run(run_id)
    if not run:
        raise ValueError(f"未找到运行记录：{run_id}")
    definition = repository.read_experiment_definition(run["experiment_id"])
    source_path = Path(definition.get("source_path", "")) if definition else Path()
    if not source_path.exists():
        raise ValueError("源 experiment definition 不可用，无法创建可运行的后续实验")
    original = load_experiment(source_path)
    persisted_case = repository.get_case(run["case_id"], run["case_revision"])
    if not persisted_case:
        raise ValueError("源 Case revision 不可用，无法创建相同 revision 的后续实验")
    cases = tuple(
        persisted_case if case.id == persisted_case.id else case
        for case in original.suite.cases
    )
    suite = SuiteSpec(original.suite.id, original.suite.kind, cases)
    packet = build_diagnosis_packet(repository, run_id)
    changed = ((packet.get("best_next_experiment") or {}).get("proposed_independent_variable") or "UNSPECIFIED")
    follow_up = ExperimentSpec(
        id=f"follow-up-{original.id}-{run_id[:8]}",
        suite=suite,
        variants=original.variants,
        trials=max(5, original.trials),
        max_concurrency=original.max_concurrency,
        source_path=source_path,
        metadata={
            **original.metadata,
            "draft": True,
            "requires_user_confirmation": True,
            "follow_up_of_run": run_id,
            "proposed_independent_variable": changed,
            "evidence_before_interpretation": True,
        },
    )
    repository.save_experiment(follow_up, status="DRAFT", follow_up_of=run_id)
    return follow_up
