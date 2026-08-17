from __future__ import annotations

import json
from typing import Any, Sequence

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExportResult,
    SpanExporter,
)
from opentelemetry.trace import Status, StatusCode, set_span_in_context


_AEL_VERSION = "0.1.0"


def _trace_endpoint(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    for suffix in ("/v1/traces", "/v1/logs", "/v1/metrics"):
        if base.endswith(suffix):
            return f"{base[:-len(suffix)]}/v1/traces"
    return f"{base}/v1/traces"


def _span_attribute(value: Any) -> str | bool | int | float | Sequence[str | bool | int | float]:
    if isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


class _RecordingExporter(SpanExporter):
    """Record exporter outcomes while delegating OTLP to the official exporter."""

    def __init__(self, delegate: SpanExporter):
        self.delegate = delegate
        self.exported_span_count = 0
        self.successful_span_count = 0
        self.last_result: SpanExportResult | None = None

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.exported_span_count += len(spans)
        result = self.delegate.export(spans)
        self.last_result = result
        if result == SpanExportResult.SUCCESS:
            self.successful_span_count += len(spans)
        return result

    def shutdown(self) -> None:
        self.delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.delegate.force_flush(timeout_millis)


class AELLifecycle:
    """Create AEL-owned lifecycle spans with the official OpenTelemetry SDK."""

    def __init__(self, *, endpoint: str | None, resource: dict[str, Any]):
        self.endpoint = str(endpoint or "").strip() or None
        self.trace_id: str | None = None
        resource_attributes = {
            "service.name": "agent-eval-lab",
            "service.version": _AEL_VERSION,
            "ael.lifecycle": True,
            **resource,
        }
        provider = TracerProvider(resource=Resource.create(resource_attributes))
        self._exporter: _RecordingExporter | None = None
        if self.endpoint:
            self._exporter = _RecordingExporter(
                OTLPSpanExporter(endpoint=_trace_endpoint(self.endpoint), timeout=4)
            )
            provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        self._provider = provider
        self._tracer = provider.get_tracer("ael.lifecycle", _AEL_VERSION)
        self.spans: list[Span] = []
        self._by_id: dict[str, Span] = {}
        self._descriptions: dict[str, dict[str, Any]] = {}

    def start(
        self,
        name: str,
        *,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        parent = self._by_id.get(parent_span_id) if parent_span_id else None
        context = set_span_in_context(parent) if parent else None
        span = self._tracer.start_span(name, context=context)
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(str(key), _span_attribute(value))
        span_id = f"{span.context.span_id:016x}"
        if self.trace_id is None:
            self.trace_id = f"{span.context.trace_id:032x}"
        self.spans.append(span)
        self._by_id[span_id] = span
        self._descriptions[span_id] = {
            "name": name,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "trace_id": self.trace_id,
            "attributes": dict(attributes or {}),
            "status": "UNSET",
        }
        return span_id

    def end(
        self,
        span_id: str,
        *,
        error: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        span = self._by_id.get(span_id)
        description = self._descriptions.get(span_id)
        if span is None or description is None:
            return
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(str(key), _span_attribute(value))
                description["attributes"][str(key)] = value
        if error:
            span.set_status(Status(StatusCode.ERROR, error[:240]))
            description["status"] = "ERROR"
            description["status_message"] = error[:240]
        else:
            span.set_status(Status(StatusCode.OK))
            description["status"] = "OK"
        span.end()

    def span_snapshot(self) -> list[dict[str, Any]]:
        return [dict(self._descriptions[f"{span.context.span_id:016x}"]) for span in self.spans]

    def export(self) -> dict[str, Any]:
        span_count = len(self.spans)
        result: dict[str, Any] = {
            "source": "official-opentelemetry-sdk",
            "exporter": "opentelemetry-exporter-otlp-proto-http" if self.endpoint else None,
            "trace_id": self.trace_id,
            "span_count": span_count,
            "span_names": [span.name for span in self.spans],
            "endpoint_configured": bool(self.endpoint),
            "exported": False,
            "export_result": "NOT_CONFIGURED" if not self.endpoint else "UNKNOWN",
            "evidence": "not exported; AEL_OTEL_ENDPOINT is not configured",
        }
        if not self.endpoint or not self.spans or self._exporter is None:
            return result
        flushed = self._provider.force_flush(timeout_millis=4000)
        successful = flushed and self._exporter.successful_span_count >= span_count
        export_result = self._exporter.last_result.name if self._exporter.last_result else "UNKNOWN"
        result.update(
            {
                "exported": successful,
                "export_result": export_result,
                "evidence": (
                    "AEL lifecycle spans exported by the official OTLP exporter; "
                    "vendor traces remain separately correlated by ael.run.id"
                    if successful
                    else "AEL lifecycle spans were not accepted by the configured official OTLP exporter"
                ),
            }
        )
        return result
