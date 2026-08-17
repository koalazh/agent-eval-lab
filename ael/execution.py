from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .hashing import UNKNOWN, canonical_json, sha256_text
from .redaction import redact


_DYNAMIC_ENV_KEYS = {
    "AEL_EXPERIMENT_ID",
    "AEL_RUN_ID",
    "AEL_CASE_ID",
    "AEL_VARIANT_ID",
    "AEL_TRIAL",
    "OTEL_RESOURCE_ATTRIBUTES",
}


def _normalise_argument(value: str, *, workspace: str, prompt: str) -> str:
    if value == prompt:
        return "<prompt>"
    if value == workspace:
        return "<workspace>"
    normalized = value.replace(workspace, "<workspace>")
    return normalized.replace(prompt, "<prompt>") if prompt else normalized


def make_execution_receipt(
    *,
    argv: list[str],
    cwd: Path,
    prompt: str,
    prompt_transport: str,
    env: Mapping[str, str],
    base_env: Mapping[str, str] | None = None,
    resolved_executable: str | None = None,
) -> dict[str, Any]:
    """Build the receipt from the exact process inputs a driver is about to use."""
    workspace = str(cwd)
    base = dict(base_env or os.environ)
    environment_delta = {
        str(key): redact(str(value))
        for key, value in sorted(env.items())
        if base.get(key) != value
    }
    comparison_environment = {
        key: value
        for key, value in environment_delta.items()
        if key not in _DYNAMIC_ENV_KEYS
    }
    normalized_argv = [
        _normalise_argument(str(value), workspace=workspace, prompt=prompt)
        for value in argv
    ]
    comparison_payload = {
        "resolved_executable": str(resolved_executable or (argv[0] if argv else UNKNOWN)),
        "argv": normalized_argv,
        "prompt_hash": sha256_text(prompt),
        "prompt_transport": prompt_transport,
        "environment_delta": comparison_environment,
    }
    return {
        "resolved_executable": comparison_payload["resolved_executable"],
        "argv": [redact(str(value)) for value in argv],
        "normalized_argv": normalized_argv,
        "cwd": workspace,
        "prompt_hash": comparison_payload["prompt_hash"],
        "prompt_transport": prompt_transport,
        "relevant_environment_delta": environment_delta,
        "comparison_payload": comparison_payload,
        "effective_execution_hash": sha256_text(canonical_json(comparison_payload)),
    }
