from __future__ import annotations

import json
from typing import Any

from .models import ObservationProfile
from .redaction import redact, redact_text


_SENSITIVE_KEYS = {
    "prompt",
    "prompts",
    "input",
    "inputs",
    "content",
    "arguments",
    "args",
    "tool_args",
    "tool_arguments",
    "tool_result",
    "tool_results",
    "command",
    "commands",
    "result",
    "results",
    "transcript",
    "messages",
}


def _strip_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[已省略：当前 observation profile 不采集此字段]"
            if key.lower() in _SENSITIVE_KEYS
            else _strip_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_strip_sensitive(item) for item in value]
    return value


def filter_jsonl(text: str, profile: ObservationProfile) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            lines.append(redact_text(line))
            continue
        value = raw if profile == ObservationProfile.DEEP else _strip_sensitive(raw)
        lines.append(json.dumps(redact(value), sort_keys=True, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def filter_event_data(data: dict[str, Any], profile: ObservationProfile) -> dict[str, Any]:
    value = data if profile == ObservationProfile.DEEP else _strip_sensitive(data)
    return redact(value)
