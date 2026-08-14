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
    observed = [
        f"运行状态为 {candidate.get('status', 'UNKNOWN')}。",
        f"Verifier 任务结果为 {candidate.get('outcome', 'UNKNOWN')}。",
    ]
    evidence: list[str] = []
    counter_evidence: list[str] = []
    unknowns: list[str] = []
    artifact = details.get("artifact_diff", {})
    changes = artifact.get("changes", {})
    if changes:
        evidence.append(f"Workspace 变化文件：{changes.get('changed_files', [])}。")
    if details.get("matched_reference"):
        evidence.append(f"匹配的 PASS reference：{details['matched_reference']['run_id']}。")
        scope = details.get("variable_scope") or {}
        if scope.get("changed"):
            evidence.append(f"记录到的变化变量：{scope['changed']}。")
        if scope.get("same"):
            counter_evidence.append(f"记录到的相同变量：{scope['same']}。")
    else:
        unknowns.append("没有足够接近且 revision 相同的 PASS reference。")
    divergence = details.get("first_meaningful_divergence", {})
    if divergence.get("status") == "DIVERGENCE":
        evidence.append(f"Anchor sequence 在索引 {divergence.get('anchor_index')} 处分歧：{divergence}。")
    else:
        unknowns.append("normalized anchor sequence 中没有找到明确的有意义分歧。")
    coverage = details.get("evidence_coverage", {})
    for name, value in coverage.items():
        if value in {"?", "✗"}:
            unknowns.append(f"{name} 的 evidence coverage 为 {value}。")
    hypotheses = [
        {
            "statement": "在记录的实验条件下，候选运行结果与匹配的 reference 不同。",
            "evidence": evidence,
            "counter_evidence": counter_evidence,
            "certainty": "仅为描述性假设",
        }
    ]
    changed = ((details.get("variable_scope") or {}).get("changed") or [])
    proposed_variable = changed[0] if len(changed) == 1 else "UNSPECIFIED"
    return {
        "observed": observed,
        "hypotheses": hypotheses,
        "evidence": evidence,
        "counter_evidence": counter_evidence,
        "unknowns": sorted(set(unknowns)),
        "suggested_improvement": "在隔离拟议变量并保持相同 trial policy 的情况下，重跑同一 Case revision。",
        "best_next_experiment": {
            "status": "DRAFT",
            "requires_user_confirmation": True,
            "proposed_independent_variable": proposed_variable,
            "same_case_revision": bool(candidate.get("case_revision")),
            "trials": 5,
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
