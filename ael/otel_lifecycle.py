from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


_AEL_VERSION = "0.1.0"


def _attribute_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": str(key), "value": _attribute_value(value)}
        for key, value in values.items()
        if value is not None
    ]


def _trace_endpoint(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    for suffix in ("/v1/traces", "/v1/logs", "/v1/metrics"):
        if base.endswith(suffix):
            return f"{base[:-len(suffix)]}/v1/traces"
    return f"{base}/v1/traces"


@dataclass
class _Span:
    name: str
    span_id: str
    parent_span_id: str | None
    start_ns: int
    attributes: dict[str, Any] = field(default_factory=dict)
    end_ns: int | None = None
    status_code: int = 0
    status_message: str | None = None

    def to_otlp(self, trace_id: str) -> dict[str, Any]:
        span = {
            "traceId": trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": 1,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(self.end_ns or self.start_ns),
            "attributes": _attributes(self.attributes),
            "status": {"code": self.status_code},
        }
        if self.parent_span_id:
            span["parentSpanId"] = self.parent_span_id
        if self.status_message:
            span["status"]["message"] = self.status_message
        return span


class AELLifecycle:
    """Emit AEL-owned lifecycle spans without linking them to vendor spans."""

    def __init__(self, *, endpoint: str | None, resource: dict[str, Any]):
        self.endpoint = str(endpoint or "").strip() or None
        self.trace_id = uuid.uuid4().hex
        self.resource = {
            "service.name": "agent-eval-lab",
            "service.version": _AEL_VERSION,
            "ael.lifecycle": True,
            **resource,
        }
        self.spans: list[_Span] = []

    def start(
        self,
        name: str,
        *,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        span_id = uuid.uuid4().hex[:16]
        self.spans.append(
            _Span(
                name=name,
                span_id=span_id,
                parent_span_id=parent_span_id,
                start_ns=time.time_ns(),
                attributes=dict(attributes or {}),
            )
        )
        return span_id

    def end(
        self,
        span_id: str,
        *,
        error: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        span = next((item for item in self.spans if item.span_id == span_id), None)
        if not span:
            return
        span.end_ns = time.time_ns()
        if attributes:
            span.attributes.update(attributes)
        if error:
            span.status_code = 2
            span.status_message = error[:240]
        else:
            span.status_code = 1

    def payload(self) -> dict[str, Any]:
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": _attributes(self.resource)},
                    "scopeSpans": [
                        {
                            "scope": {"name": "ael.lifecycle", "version": _AEL_VERSION},
                            "spans": [span.to_otlp(self.trace_id) for span in self.spans],
                        }
                    ],
                }
            ]
        }

    def export(self) -> dict[str, Any]:
        result = {
            "source": "ael.lifecycle",
            "trace_id": self.trace_id,
            "span_count": len(self.spans),
            "endpoint_configured": bool(self.endpoint),
            "exported": False,
            "evidence": "not exported; AEL_OTEL_ENDPOINT is not configured",
        }
        if not self.endpoint or not self.spans:
            return result
        request = Request(
            _trace_endpoint(self.endpoint),
            data=json.dumps(self.payload(), separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=4):
                pass
        except (OSError, URLError) as exc:
            result.update(
                {
                    "evidence": "AEL lifecycle spans were not accepted by the configured OTLP endpoint",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return result
        result.update(
            {
                "exported": True,
                "evidence": "AEL lifecycle spans exported via OTLP; vendor traces remain separately correlated by ael.run.id",
            }
        )
        return result
