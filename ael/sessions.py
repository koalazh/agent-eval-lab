from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .otel_ingest import (
    _SIGNAL_FILES,
    _attributes,
    _metric_points,
    _otlp_value,
    _record_event,
    _resource_attributes,
    _summary,
    _text,
    _timestamp,
)
from .redaction import redact


_SESSION_KEYS = ("session.id", "session_id")
_CWD_KEYS = ("cwd", "working_directory", "project.cwd", "project.path", "project")


def _value(attrs: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        raw = attrs.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def _session_id(resource: dict[str, Any], attrs: dict[str, Any]) -> str | None:
    return _value(attrs, _SESSION_KEYS) or _value(resource, _SESSION_KEYS)


def _timestamp_value(record: dict[str, Any], attrs: dict[str, Any]) -> tuple[float, str] | None:
    raw = _timestamp(record) or _value(attrs, ("event.timestamp",))
    if raw is None:
        return None
    try:
        number = float(raw)
        if number > 10_000_000_000:
            return number, datetime.fromtimestamp(number / 1_000_000_000, timezone.utc).isoformat()
        return number, datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp(), parsed.astimezone(timezone.utc).isoformat()


def _metric_value(record: dict[str, Any]) -> Any:
    for key in ("asInt", "asDouble", "count", "sum"):
        if record.get(key) is not None:
            return record[key]
    return None


@dataclass
class _SessionAccumulator:
    vendor_session_id: str
    events: list[Any] = field(default_factory=list)
    raw_records: list[dict[str, Any]] = field(default_factory=list)
    signals: Counter[str] = field(default_factory=Counter)
    records: Counter[str] = field(default_factory=Counter)
    run_ids: set[str] = field(default_factory=set)
    timestamps: list[tuple[float, str]] = field(default_factory=list)
    agent: str = "UNKNOWN"
    agent_version: str = "UNKNOWN"
    cwd: str = "UNKNOWN"

    def add(
        self,
        signal: str,
        container: dict[str, Any],
        record: dict[str, Any],
        attrs: dict[str, Any],
        *,
        name: str,
        body: str = "",
    ) -> None:
        resource = _resource_attributes(container)
        event_attrs = dict(attrs)
        event_attrs.setdefault("session.id", self.vendor_session_id)
        event = _record_event(signal, record, resource, name=name, attributes=event_attrs, body=body)
        self.events.append(event)
        self.raw_records.append(
            redact(
                {
                    "signal": signal,
                    "resource": resource,
                    "record": record,
                    "attributes": event_attrs,
                    "name": name,
                }
            )
        )
        self.signals[signal] += 1
        self.records[signal] += 1
        for source in (resource, attrs):
            run_id = source.get("ael.run.id")
            if run_id:
                self.run_ids.add(str(run_id))
            self.agent = str(source.get("service.name") or self.agent)
            self.agent_version = str(source.get("service.version") or self.agent_version)
            self.cwd = _value(source, _CWD_KEYS) or self.cwd
        timestamp = _timestamp_value(record, attrs)
        if timestamp:
            self.timestamps.append(timestamp)

    def to_dict(self, managed_runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        run_id = next(iter(sorted(self.run_ids)), None)
        managed_run = managed_runs.get(run_id) if run_id else None
        if run_id:
            origin = (
                f"Managed by Experiment {managed_run['experiment_id']}"
                if managed_run
                else f"Managed by AEL Run {run_id}"
            )
        else:
            origin = "External terminal（不是 AEL 发起）"
        telemetry = _summary(self.events, self.vendor_session_id, self.records)
        telemetry.update(
            {
                "source": "otel_collector_session",
                "session_id": self.vendor_session_id,
                "correlation": "session.id",
                "evidence": "real OTLP records correlated by session.id" if self.events else "insufficient evidence",
            }
        )
        started_at = min(self.timestamps)[1] if self.timestamps else "UNKNOWN"
        ended_at = max(self.timestamps)[1] if self.timestamps else "UNKNOWN"
        return {
            "vendor_session_id": self.vendor_session_id,
            "agent": self.agent,
            "agent_version": self.agent_version,
            "started_at": started_at,
            "ended_at": ended_at,
            "cwd": self.cwd,
            "origin": origin,
            "managed": bool(run_id),
            "managed_run_id": run_id,
            "evidence_ref": ".ael/otel/{logs,metrics,traces}.jsonl · session.id",
            "signals": dict(self.signals),
            "event_count": len(self.events),
            "outcome": "UNVERIFIED",
            "telemetry": {"otel": telemetry},
            "events": [event.to_dict() for event in self.events],
            "raw": json.dumps(self.raw_records, ensure_ascii=False, indent=2, sort_keys=True),
        }


def _iter_log_records(payload: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]]:
    for container in payload.get("resourceLogs", []):
        if not isinstance(container, dict):
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
                yield container, record, attrs, name, _text(record.get("body"))


def _iter_metric_records(payload: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]]:
    for container in payload.get("resourceMetrics", []):
        if not isinstance(container, dict):
            continue
        for scope in container.get("scopeMetrics", []):
            if not isinstance(scope, dict):
                continue
            for metric in scope.get("metrics", []):
                if not isinstance(metric, dict):
                    continue
                name = str(metric.get("name") or "metric")
                for point in _metric_points(metric):
                    attrs = _attributes(point.get("attributes", []))
                    value = _metric_value(point)
                    body = f"{name}={_otlp_value(value)}" if value is not None else name
                    yield container, point, attrs, name, body


def _iter_span_records(payload: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]]:
    for container in payload.get("resourceSpans", []):
        if not isinstance(container, dict):
            continue
        for scope in container.get("scopeSpans", []):
            if not isinstance(scope, dict):
                continue
            for span in scope.get("spans", []):
                if not isinstance(span, dict):
                    continue
                attrs = _attributes(span.get("attributes", []))
                yield container, span, attrs, str(span.get("name") or "span"), ""


_ITERATORS = {
    "logs": _iter_log_records,
    "metrics": _iter_metric_records,
    "traces": _iter_span_records,
}


def discover_sessions(
    root: Path,
    managed_runs: list[dict[str, Any]] | None = None,
    *,
    include_managed: bool = False,
) -> list[dict[str, Any]]:
    """Project sessions directly from Collector evidence without a second store."""
    accumulators: dict[str, _SessionAccumulator] = {}
    collector_dir = root.resolve() / ".ael" / "otel"
    for signal, filename in _SIGNAL_FILES.items():
        path = collector_dir / filename
        if not path.exists():
            continue
        iterator = _ITERATORS[signal]
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            for container, record, attrs, name, body in iterator(payload):
                resource = _resource_attributes(container)
                session_id = _session_id(resource, attrs)
                if not session_id:
                    continue
                accumulator = accumulators.setdefault(session_id, _SessionAccumulator(session_id))
                accumulator.add(signal, container, record, attrs, name=name, body=body)

    managed = {str(run["id"]): run for run in (managed_runs or []) if run.get("id")}
    sessions = [item.to_dict(managed) for item in accumulators.values()]
    if not include_managed:
        sessions = [item for item in sessions if not item["managed"]]
    return sorted(sessions, key=lambda item: (item["ended_at"] == "UNKNOWN", item["ended_at"], item["vendor_session_id"]), reverse=True)


def get_session(
    root: Path,
    vendor_session_id: str,
    managed_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in discover_sessions(root, managed_runs, include_managed=True)
            if item["vendor_session_id"] == vendor_session_id
        ),
        None,
    )
