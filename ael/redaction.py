from __future__ import annotations

import json
import re
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|authorization|bearer|token|password|secret)(\s*[:=]\s*)([A-Za-z0-9_./+=:-]{8,})"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:ghp|github_pat|xoxb|xoxp|AIza)[A-Za-z0-9_-]{12,}\b"),
)


def redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            result = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def redact_json(value: Any) -> str:
    return json.dumps(redact(value), sort_keys=True, indent=2, ensure_ascii=False, default=str)

