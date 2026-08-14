from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .cases import ExperimentSpec, load_experiment
from .comparison import compare_run_details
from .persistence import Repository
from .redaction import redact


def build_diagnosis_packet(repository: Repository, run_id: str) -> dict[str, Any]:
    details = compare_run_details(repository, run_id)
    if not details:
        return {}
    candidate = details.get("candidate", {})
    observed = [
        f"Run status is {candidate.get('status', 'UNKNOWN')}.",
        f"Verifier task outcome is {candidate.get('outcome', 'UNKNOWN')}.",
    ]
    evidence: list[str] = []
    counter_evidence: list[str] = []
    unknowns: list[str] = []
    artifact = details.get("artifact_diff", {})
    changes = artifact.get("changes", {})
    if changes:
        evidence.append(f"Workspace changed files: {changes.get('changed_files', [])}.")
    if details.get("matched_reference"):
        evidence.append(f"Matched PASS reference: {details['matched_reference']['run_id']}.")
        scope = details.get("variable_scope") or {}
        if scope.get("changed"):
            evidence.append(f"Recorded changed variables: {scope['changed']}.")
        if scope.get("same"):
            counter_evidence.append(f"Recorded same variables: {scope['same']}.")
    else:
        unknowns.append("No sufficiently close same-revision PASS reference was available.")
    divergence = details.get("first_meaningful_divergence", {})
    if divergence.get("status") == "DIVERGENCE":
        evidence.append(f"Anchor divergence at index {divergence.get('anchor_index')}: {divergence}.")
    else:
        unknowns.append("No clear meaningful divergence was found in the normalized anchor sequence.")
    coverage = details.get("evidence_coverage", {})
    for name, value in coverage.items():
        if value in {"?", "✗"}:
            unknowns.append(f"Evidence coverage for {name} is {value}.")
    hypotheses = [
        {
            "statement": "The candidate outcome differs from the matched reference under the recorded experiment conditions.",
            "evidence": evidence,
            "counter_evidence": counter_evidence,
            "certainty": "descriptive hypothesis only",
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
        "suggested_improvement": "Repeat the same Case revision with the proposed variable isolated and the same trial policy.",
        "best_next_experiment": {
            "status": "DRAFT",
            "requires_user_confirmation": True,
            "proposed_independent_variable": proposed_variable,
            "same_case_revision": candidate.get("outcome") is not None,
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
        raise ValueError(f"run not found: {run_id}")
    definition = repository.read_experiment_definition(run["experiment_id"])
    source_path = Path(definition.get("source_path", "")) if definition else Path()
    if not source_path.exists():
        raise ValueError("source experiment definition is unavailable; cannot create a runnable follow-up")
    original = load_experiment(source_path)
    packet = build_diagnosis_packet(repository, run_id)
    changed = ((packet.get("best_next_experiment") or {}).get("proposed_independent_variable") or "UNSPECIFIED")
    follow_up = ExperimentSpec(
        id=f"follow-up-{original.id}-{run_id[:8]}",
        suite=original.suite,
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

