from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .models import ObservableEvent
from .redaction import redact


_SIGNAL_FILES = {
    "traces": "traces.jsonl",
    "metrics": "metrics.jsonl",
    "logs": "logs.jsonl",
}


def _otlp_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in (
        "stringValue",
        "boolValue",
        "intValue",
        "doubleValue",
        "bytesValue",
        "jsonValue",
    ):
        if key in value:
            return value[key]
    if "arrayValue" in value and isinstance(value["arrayValue"], dict):
        return [_otlp_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value and isinstance(value["kvlistValue"], dict):
        return _attributes(value["kvlistValue"].get("values", []))
    return value


def _attributes(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return {str(key): _otlp_value(value) for key, value in raw.items()}
    if not isinstance(raw, list):
        return {}
    result: dict[str, Any] = {}
    for item in raw:
        if not isinstance(item, dict) or "key" not in item:
            continue
        result[str(item["key"])] = _otlp_value(item.get("value"))
    return result


def _resource_attributes(container: dict[str, Any]) -> dict[str, Any]:
    resource = container.get("resource")
    return _attributes(resource.get("attributes", [])) if isinstance(resource, dict) else {}


def _matches_run(container: dict[str, Any], run_id: str) -> bool:
    return _resource_attributes(container).get("ael.run.id") == run_id


def _text(value: Any) -> str:
    value = _otlp_value(value)
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return str(value)


def _event_kind(name: str, attributes: dict[str, Any], body: str) -> str:
    text = " ".join((name, body, *(str(value) for value in attributes.values()))).lower()
    if any(token in text for token in ("tool_call", "tool-use", "tool use", "tool.call", "tool_decision")):
        return "tool_call"
    if any(token in text for token in ("tool_result", "tool-result", "tool result", "tool.result")):
        return "tool_result"
    if any(token in text for token in ("command_execution", "command-execution", "shell", "bash")):
        return "command"
    if any(token in text for token in ("file_change", "file-change", "edit", "patch")):
        return "file_change"
    if any(token in text for token in ("verification", "verify", "pytest", "test")):
        return "verification"
    if any(token in text for token in ("complete", "completion", "settled", "agent_end", "agent.end")):
        return "final"
    return "message"


def _timestamp(record: dict[str, Any]) -> str | None:
    for key in ("timeUnixNano", "observedTimeUnixNano", "startTimeUnixNano"):
        if record.get(key) is not None:
            return str(record[key])
    return None


def _record_event(
    signal: str,
    record: dict[str, Any],
    resource: dict[str, Any],
    *,
    name: str = "",
    attributes: dict[str, Any] | None = None,
    body: str = "",
) -> ObservableEvent:
    attrs = attributes or {}
    summary = body or name or signal
    return ObservableEvent(
        kind=_event_kind(name, attrs, body),
        name=name or None,
        summary=redact(summary),
        source="otel",
        timestamp=_timestamp(record),
        data=redact(
            {
                "signal": signal,
                "resource": resource,
                "attributes": attrs,
                "record": record,
            }
        ),
    )


def _log_events(payload: dict[str, Any], run_id: str) -> list[ObservableEvent]:
    result: list[ObservableEvent] = []
    for container in payload.get("resourceLogs", []):
        if not isinstance(container, dict) or not _matches_run(container, run_id):
            continue
        resource = _resource_attributes(container)
        for scope in container.get("scopeLogs", []):
            if not isinstance(scope, dict):
                continue
            for record in scope.get("logRecords", []):
                if not isinstance(record, dict):
                    continue
                attrs = _attributes(record.get("attributes", []))
                name = str(attrs.get("event.name") or attrs.get("name") or "log")
                body = _text(record.get("body"))
                result.append(_record_event("logs", record, resource, name=name, attributes=attrs, body=body))
    return result


def _metric_points(metric: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("gauge", "sum", "histogram", "exponentialHistogram", "summary"):
        value = metric.get(key)
        if isinstance(value, dict) and isinstance(value.get("dataPoints"), list):
            return [item for item in value["dataPoints"] if isinstance(item, dict)]
    return []


def _metric_events(payload: dict[str, Any], run_id: str) -> list[ObservableEvent]:
    result: list[ObservableEvent] = []
    for container in payload.get("resourceMetrics", []):
        if not isinstance(container, dict) or not _matches_run(container, run_id):
            continue
        resource = _resource_attributes(container)
        for scope in container.get("scopeMetrics", []):
            if not isinstance(scope, dict):
                continue
            for metric in scope.get("metrics", []):
                if not isinstance(metric, dict):
                    continue
                name = str(metric.get("name") or "metric")
                for point in _metric_points(metric):
                    attrs = _attributes(point.get("attributes", []))
                    value = next(
                        (
                            point.get(key)
                            for key in ("asInt", "asDouble", "count", "sum")
                            if point.get(key) is not None
                        ),
                        None,
                    )
                    body = f"{name}={value}" if value is not None else name
                    result.append(_record_event("metrics", point, resource, name=name, attributes=attrs, body=body))
    return result


def _span_events(payload: dict[str, Any], run_id: str) -> list[ObservableEvent]:
    result: list[ObservableEvent] = []
    for container in payload.get("resourceSpans", []):
        if not isinstance(container, dict) or not _matches_run(container, run_id):
            continue
        resource = _resource_attributes(container)
        for scope in container.get("scopeSpans", []):
            if not isinstance(scope, dict):
                continue
            for span in scope.get("spans", []):
                if not isinstance(span, dict):
                    continue
                attrs = _attributes(span.get("attributes", []))
                name = str(span.get("name") or "span")
                result.append(_record_event("traces", span, resource, name=name, attributes=attrs))
    return result


def _events_for_payload(signal: str, payload: dict[str, Any], run_id: str) -> list[ObservableEvent]:
    if signal == "logs":
        return _log_events(payload, run_id)
    if signal == "metrics":
        return _metric_events(payload, run_id)
    return _span_events(payload, run_id)


def _numeric_attributes(event: ObservableEvent) -> dict[str, float]:
    values: dict[str, float] = {}
    attributes = event.data.get("attributes", {}) if isinstance(event.data, dict) else {}
    if not isinstance(attributes, dict):
        return values
    for key, value in attributes.items():
        try:
            values[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return values


def _summary(events: list[ObservableEvent], run_id: str, records: Counter[str]) -> dict[str, Any]:
    counts = Counter(event.kind for event in events)
    input_tokens = 0.0
    output_tokens = 0.0
    duration_ms = 0.0
    for event in events:
        for key, value in _numeric_attributes(event).items():
            lowered = key.lower().replace("-", "_")
            if "input" in lowered and "token" in lowered:
                input_tokens += value
            elif "output" in lowered and "token" in lowered:
                output_tokens += value
            elif "duration" in lowered and ("ms" in lowered or "millisecond" in lowered):
                duration_ms += value
    result: dict[str, Any] = {
        "source": "otel_collector",
        "run_id": run_id,
        "records": dict(records),
        "events": len(events),
        "event_kinds": dict(counts),
        "model_calls": sum(
            1
            for event in events
            if any(token in (event.name or "").lower() for token in ("llm", "model", "generation", "api_request"))
        ),
        "tool_calls": counts.get("tool_call", 0),
        "input_tokens": int(input_tokens) if input_tokens.is_integer() else input_tokens,
        "output_tokens": int(output_tokens) if output_tokens.is_integer() else output_tokens,
        "duration_ms": int(duration_ms) if duration_ms.is_integer() else duration_ms,
        "evidence": "real OTLP records correlated by ael.run.id" if events else "insufficient evidence",
    }
    return result


def ingest_collector_output(root: Path, run_id: str, evidence_dir: Path) -> tuple[list[ObservableEvent], dict[str, Any]]:
    """Consume only Collector records carrying this run's resource attribute."""
    collector_dir = root / ".ael" / "otel"
    otel_dir = evidence_dir / "telemetry" / "otel"
    otel_dir.mkdir(parents=True, exist_ok=True)
    events: list[ObservableEvent] = []
    raw_records: list[dict[str, Any]] = []
    records: Counter[str] = Counter()
    for signal, filename in _SIGNAL_FILES.items():
        path = collector_dir / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            correlated = _events_for_payload(signal, payload, run_id)
            if not correlated:
                continue
            records[signal] += 1
            raw_records.append({"signal": signal, "payload": redact(payload)})
            events.extend(correlated)
    (otel_dir / "raw.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n" for record in raw_records),
        encoding="utf-8",
    )
    (otel_dir / "events.jsonl").write_text(
        "".join(json.dumps(event.to_dict(), sort_keys=True, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    summary = _summary(events, run_id, records)
    (otel_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return events, summary
