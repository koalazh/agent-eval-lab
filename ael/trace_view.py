from __future__ import annotations

from collections import Counter
from datetime import datetime
import re
from typing import Any


_SIGNAL_LABELS = {
    "traces": "OTel trace / span",
    "logs": "OTel 日志",
    "metrics": "OTel 指标",
    "native": "原生记录",
    "verifier": "Verifier",
    "workspace": "Workspace",
}

_KIND_LABELS = {
    "tool_call": "工具调用",
    "tool_result": "工具结果",
    "command": "命令执行",
    "file_change": "文件变更",
    "verification": "验证结果",
    "final": "Agent 完成",
    "message": "消息事件",
    "unknown": "未归一化事件",
}

_SAFE_ATTRIBUTE_KEYS = (
    "event.sequence",
    "event.name",
    "trace_id",
    "span_id",
    "parent_span_id",
    "span.kind",
    "span.status",
    "status.message",
    "tool_name",
    "tool_source",
    "tool_use_id",
    "model",
    "query_source",
    "duration_ms",
    "success",
    "decision",
    "language",
    "type",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "response_length",
    "prompt_length",
    "effort",
)

_READ_TOOLS = {"read", "glob", "grep", "ls", "find", "search", "cat", "head", "tail"}
_MUTATE_TOOLS = {"edit", "write", "apply_patch", "applypatch", "notebookedit"}


_FILE_PATH_KEYS = {
    "path",
    "filepath",
    "file_path",
    "filename",
    "file_name",
}


def _file_paths(value: Any, *, key: str = "") -> list[str]:
    """Extract only explicit file path fields; never infer a path from text."""
    paths: list[str] = []
    normalized_key = key.lower().replace("-", "_")
    if normalized_key in _FILE_PATH_KEYS and isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            paths.extend(_file_paths(child_value, key=str(child_key)))
    elif isinstance(value, list):
        for child_value in value:
            paths.extend(_file_paths(child_value, key=key))
    return paths


def _event_file_paths(event: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(_file_paths(_data(event))))


def _file_label(path: str, changed_files: list[str]) -> str:
    """Prefer the Workspace-relative name when native evidence uses a temp path."""
    raw = path.strip()
    for changed in changed_files:
        changed_text = str(changed)
        if raw == changed_text or raw.endswith("/" + changed_text) or raw.endswith("\\" + changed_text):
            return changed_text
        if raw.rsplit("/", 1)[-1] == changed_text.rsplit("/", 1)[-1]:
            return changed_text
    return raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or raw


def _tool_operation(tool: str, change_kind: str | None = None) -> str | None:
    if change_kind:
        normalized = change_kind.lower()
        if normalized in {"create", "created", "add", "added"}:
            return "create"
        if normalized in {"delete", "deleted", "remove", "removed"}:
            return "delete"
        if normalized in {"update", "updated", "edit", "edited", "modify", "modified"}:
            return "update"
    normalized = tool.strip().lower().replace("-", "_")
    if normalized in _READ_TOOLS:
        return "read"
    if normalized in _MUTATE_TOOLS:
        return "update"
    if normalized in {"delete", "remove", "rm"}:
        return "delete"
    return None


def _native_tool_name(event: dict[str, Any]) -> str:
    name = _event_name(event).strip()
    if name and name.lower() not in {"tool", "tool_decision", "tool_result"}:
        return name
    return _tool_name(event)


def _data(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("data")
    return value if isinstance(value, dict) else {}


def _attributes(event: dict[str, Any]) -> dict[str, Any]:
    value = _data(event).get("attributes")
    return value if isinstance(value, dict) else {}


def _record(event: dict[str, Any]) -> dict[str, Any]:
    value = _data(event).get("record")
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_ns(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1_000_000_000)
        except ValueError:
            return None
    return None


def _timing(event: dict[str, Any]) -> tuple[int | None, int | None]:
    data = _data(event)
    record = _record(event)
    signal = str(data.get("signal") or event.get("source") or "native")
    start = _timestamp_ns(
        record.get("startTimeUnixNano")
        or record.get("timeUnixNano")
        or event.get("timestamp")
        or data.get("timestamp")
    )
    end = _timestamp_ns(record.get("endTimeUnixNano") or record.get("timeUnixNano") or event.get("timestamp"))
    duration = _number(_attributes(event).get("duration_ms"))
    if signal == "metrics":
        return start, start
    duration_ns = int(round(duration * 1_000_000)) if duration is not None and duration >= 0 else None
    if duration is not None and duration >= 0 and start is not None and not record.get("startTimeUnixNano"):
        end = start
        start = int(start - (duration_ns or 0))
    elif duration is not None and duration >= 0 and start is not None and end is None:
        end = start + (duration_ns or 0)
    return start, end or start


def _format_duration(value: float | int | None) -> str:
    if value is None:
        return "未知"
    value = float(value)
    if value < 1:
        return "<1 ms"
    if value < 1000:
        return f"{value:.0f} ms"
    return f"{value / 1000:.2f} s"


def _format_offset(value: float | int | None) -> str:
    if value is None:
        return "无时间戳"
    value = float(value)
    if value < 1000:
        return f"+{value:.0f} ms"
    return f"+{value / 1000:.2f} s"


def _axis_ticks(total_ms: float) -> list[dict[str, str]]:
    if total_ms <= 0:
        return []
    return [
        {"left": f"{ratio * 100:.0f}", "label": _format_offset(total_ms * ratio)}
        for ratio in (0, 0.25, 0.5, 0.75, 1)
    ]


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _signal(event: dict[str, Any]) -> str:
    data = _data(event)
    if data.get("signal") in _SIGNAL_LABELS:
        return str(data["signal"])
    if event.get("source") == "otel":
        return "logs"
    return str(event.get("source") or "native")


def _event_name(event: dict[str, Any]) -> str:
    attrs = _attributes(event)
    return str(attrs.get("event.name") or event.get("name") or "")


def _tool_name(event: dict[str, Any]) -> str:
    attrs = _attributes(event)
    name = str(attrs.get("tool_name") or "").strip()
    if name:
        return name
    event_name = _event_name(event).lower()
    if "bash" in event_name:
        return "Bash"
    return "未命名工具"


def _operation(event: dict[str, Any]) -> str:
    kind = str(event.get("kind") or "unknown")
    name = _event_name(event)
    attrs = _attributes(event)
    if kind in {"tool_call", "tool_result"}:
        tool = _tool_name(event)
        return f"调用 {tool}" if kind == "tool_call" else f"{tool} 返回"
    if kind == "file_change":
        return "代码变更"
    if kind == "command":
        return "执行命令"
    if kind == "verification":
        return "验证结果"
    if name == "api_request":
        return "Model 调用"
    if name == "assistant_response":
        return "Agent 响应"
    if name == "user_prompt":
        return "任务提示"
    if name == "plugin_loaded":
        return "插件加载"
    if "token.usage" in name:
        return "Token 用量"
    if "cost.usage" in name:
        return "成本样本"
    if "lines_of_code" in name or "code_edit" in name:
        return "代码变更指标"
    if "session.count" in name:
        return "会话次数"
    if "active_time" in name:
        return "活跃时长"
    if kind == "final" and _signal(event) == "native":
        event_type = str(_data(event).get("type") or event.get("name") or "").lower()
        if event_type == "turn.started":
            return "回合开始"
        if event_type == "turn.completed":
            return "回合完成"
        if event_type == "result":
            return "Agent 返回结果"
        return "Agent 完成"
    if kind == "final":
        return "结构化 OTel 事件"
    return name or str(attrs.get("type") or _KIND_LABELS.get(kind, kind))


def _kind_label(event: dict[str, Any]) -> str:
    """Prefer the observable operation name over a generic normalized kind."""
    kind = str(event.get("kind") or "unknown")
    name = _event_name(event).lower()
    if kind in {"tool_call", "tool_result", "command", "file_change", "verification"}:
        return _KIND_LABELS.get(kind, kind)
    if name == "api_request":
        return "Model 调用"
    if name == "assistant_response":
        return "Agent 响应"
    if name == "user_prompt":
        return "任务提示"
    if name == "plugin_loaded":
        return "插件加载"
    if "token.usage" in name:
        return "Token 用量"
    if "cost.usage" in name:
        return "成本样本"
    if "lines_of_code" in name or "code_edit" in name:
        return "代码变更指标"
    if "session.count" in name:
        return "会话次数"
    if "active_time" in name:
        return "活跃时长"
    if kind == "final" and _signal(event) == "native":
        event_type = str(_data(event).get("type") or event.get("name") or "").lower()
        if event_type == "turn.started":
            return "回合开始"
        if event_type == "turn.completed":
            return "回合完成"
        if event_type == "result":
            return "Agent 返回结果"
    return _KIND_LABELS.get(kind, kind)


def _detail(event: dict[str, Any]) -> str:
    kind = str(event.get("kind") or "unknown")
    name = _event_name(event)
    attrs = _attributes(event)
    if kind in {"tool_call", "tool_result"}:
        tool = _tool_name(event)
        if kind == "tool_result":
            success = attrs.get("success")
            result = "成功" if str(success).lower() == "true" else "失败" if str(success).lower() == "false" else "已返回"
            return f"{tool} · {result} · 未采集命令内容"
        decision = attrs.get("decision")
        return f"{tool} · {('决策 ' + str(decision)) if decision else '已发起'} · 命令内容未采集"
    if name == "api_request":
        model = attrs.get("model") or "未知 Model"
        source = attrs.get("query_source") or "未知来源"
        return f"{model} · {source} · 输入 {attrs.get('input_tokens', '未知')} / 输出 {attrs.get('output_tokens', '未知')} tokens"
    if name == "assistant_response":
        return f"{attrs.get('model', '未知 Model')} · 响应已脱敏 · {attrs.get('query_source', '未知来源')}"
    if name == "user_prompt":
        return f"提示词已脱敏 · 长度 {attrs.get('prompt_length', '未知')} · sequence {attrs.get('event.sequence', '未知')}"
    if name == "plugin_loaded":
        return f"插件范围 {attrs.get('plugin.scope', '未知')} · 插件内容未展开"
    if "token.usage" in name:
        return f"{attrs.get('type', '未知')} · {attrs.get('query_source', '未知')} · 当前样本值见属性"
    if "cost.usage" in name:
        return f"{attrs.get('query_source', '未知')} · 成本样本值见属性"
    summary = str(event.get("summary") or "").strip()
    if summary and summary != name:
        return summary[:180]
    return "只显示已关联且已脱敏的可观察字段"


def _status(event: dict[str, Any]) -> tuple[str, str]:
    attrs = _attributes(event)
    if str(attrs.get("success")).lower() == "false":
        return "失败", "failed"
    if str(attrs.get("success")).lower() == "true":
        return "成功", "success"
    if attrs.get("decision"):
        return f"决策 {attrs['decision']}", "success" if str(attrs["decision"]).lower() in {"accept", "allow", "approved"} else "observed"
    status = _record(event).get("status")
    if isinstance(status, dict) and str(status.get("code", "")).upper() in {"ERROR", "STATUS_CODE_ERROR"}:
        return "错误", "failed"
    kind = str(event.get("kind") or "unknown")
    return ("已返回" if kind == "tool_result" else "已观测"), "observed"


def _span_context(event: dict[str, Any]) -> dict[str, str | int | None]:
    attrs = _attributes(event)
    record = _record(event)
    nested = record.get("spanContext") if isinstance(record.get("spanContext"), dict) else {}

    def first(*keys: str) -> Any:
        for source in (attrs, record, nested, event):
            for key in keys:
                value = source.get(key)
                if value not in (None, ""):
                    return value
        return None

    span_events = record.get("events")
    return {
        "trace_id": first("trace_id", "traceId"),
        "span_id": first("span_id", "spanId"),
        "parent_span_id": first("parent_span_id", "parentSpanId"),
        "span_kind": first("span.kind", "spanKind", "kind") if _signal(event) == "traces" else None,
        "span_event_count": len(span_events) if isinstance(span_events, list) else None,
    }


def _safe_attributes(event: dict[str, Any]) -> list[dict[str, str]]:
    attrs = _attributes(event)
    result: list[dict[str, str]] = []
    for key in _SAFE_ATTRIBUTE_KEYS:
        if key not in attrs or attrs[key] in (None, ""):
            continue
        result.append({"key": key, "value": _format_value(attrs[key])})
    record = _record(event)
    if record.get("status") and isinstance(record["status"], dict):
        result.append({"key": "span.status", "value": _format_value(record["status"].get("code", "unknown"))})
    context = _span_context(event)
    for key in ("trace_id", "span_id", "parent_span_id", "span_kind"):
        value = context.get(key)
        if value not in (None, "") and not any(item["key"] == key for item in result):
            result.append({"key": key, "value": _format_value(value)})
    return result


def _source_label(signal: str) -> str:
    return _SIGNAL_LABELS.get(signal, signal)


_VERIFIER_PHASE_LABELS = {
    "targeted verification": "定向验证",
    "visible full suite": "可见全量测试",
    "hidden boundary verification": "隐藏边界验证",
}


def _verifier_phase_label(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    return _VERIFIER_PHASE_LABELS.get(normalized, value.strip().rstrip(":"))


def _verifier_phase_detail(lines: list[str], status: str) -> str:
    compact = " ".join(line.strip() for line in lines if line.strip())
    if status == "PASS":
        matches = re.findall(r"\d+\s+(?:passed|failed|deselected|skipped)(?:,\s*\d+\s+\w+)*[^\n]*", compact)
        if matches:
            return matches[-1].strip()
        return next((line.strip() for line in reversed(lines) if line.strip()), "Verifier 阶段通过")
    if status == "FAIL":
        for line in reversed(lines):
            text = line.strip()
            if re.match(r"(?:RuntimeError|AssertionError|[A-Za-z]+Error):", text):
                return text[:220]
        return "Verifier 阶段失败"
    return "没有足够的阶段结果证据"


def build_verifier_phases(verifier: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract explicit verifier checkpoints without presenting them as Agent actions."""
    verifier = verifier or {}
    stdout = str(verifier.get("stdout") or "")
    stderr = str(verifier.get("stderr") or "")
    lines = stdout.splitlines()
    headers: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        raw_label, inline = stripped.split(":", 1)
        lowered = raw_label.lower()
        if any(token in lowered for token in ("verification", "suite", "test")):
            headers.append((index, raw_label, inline.strip()))

    phases: list[dict[str, Any]] = []
    for phase_index, (line_index, raw_label, inline) in enumerate(headers):
        end = headers[phase_index + 1][0] if phase_index + 1 < len(headers) else len(lines)
        block = ([inline] if inline else []) + lines[line_index + 1 : end]
        text = " ".join(line.strip() for line in block if line.strip()).lower()
        has_failure = bool(re.search(r"\b(?:failed|error|errors)\b", text))
        has_pass = bool(re.search(r"(?:\b\d+\s+passed\b|\bpassed\b)", text))
        status = "FAIL" if has_failure else "PASS" if has_pass else "UNKNOWN"
        phases.append(
            {
                "label": _verifier_phase_label(raw_label),
                "status": status,
                "status_class": "success" if status == "PASS" else "failed" if status == "FAIL" else "observed",
                "detail": _verifier_phase_detail(block, status),
                "source": "Verifier",
                "event_count": 1,
                "duration_label": "—",
                "sequence": "—",
                "phase": True,
            }
        )

    if verifier.get("outcome") == "FAIL" and stderr and not any("隐藏边界" in phase["label"] for phase in phases):
        error_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        detail = next(
            (line[:220] for line in reversed(error_lines) if re.match(r"(?:RuntimeError|AssertionError|[A-Za-z]+Error):", line)),
            "Verifier 最终检查失败",
        )
        phases.append(
            {
                "label": "最终边界检查",
                "status": "FAIL",
                "status_class": "failed",
                "detail": detail,
                "source": "Verifier",
                "event_count": 1,
                "duration_label": "—",
                "sequence": "—",
                "phase": True,
            }
        )
    return phases


def _merge_events(otel_events: list[dict[str, Any]], native_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not otel_events:
        return [
            event
            for event in native_events
            if str(event.get("kind") or "unknown") != "unknown"
        ]
    result = list(otel_events)
    existing = {(event.get("kind"), _event_name(event)) for event in otel_events}
    for event in native_events:
        kind = str(event.get("kind") or "unknown")
        if kind not in {"final", "command", "verification"}:
            continue
        key = (kind, _event_name(event))
        if key not in existing:
            result.append(event)
            existing.add(key)
    return result


def _ordered_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timed = [(event, index, _timing(event)[0]) for index, event in enumerate(events)]
    timed.sort(key=lambda item: (item[2] is None, item[2] or 0, item[1]))
    return [event for event, _, _ in timed]


def _trajectory_order_key(event: dict[str, Any], index: int) -> tuple[int, float, int]:
    sequence = _number(_attributes(event).get("event.sequence"))
    if sequence is not None:
        return (0, sequence, index)
    start, _ = _timing(event)
    if start is not None:
        return (1, float(start), index)
    return (2, float(index), index)


def _trace_category(event: dict[str, Any], signal: str) -> tuple[str, str]:
    kind = str(event.get("kind") or "unknown")
    name = _event_name(event).lower()
    if name in {"api_request", "assistant_response"}:
        return "model", "Model 调用"
    if kind in {"tool_call", "tool_result", "command"}:
        return "tool", "工具与命令"
    if kind == "verification":
        return "verification", "验证"
    if kind == "file_change":
        return "change", "文件变更"
    if "token" in name or "cost" in name or "lines_of_code" in name or "code_edit" in name:
        return "resource", "Token / 成本"
    if signal == "metrics":
        return "resource", "Token / 成本"
    if name in {"user_prompt", "plugin_loaded"} or kind == "final":
        return "lifecycle", "会话与生命周期"
    return "other", "其他事件"


def build_trace_view(
    otel_events: list[dict[str, Any]],
    native_events: list[dict[str, Any]],
) -> dict[str, Any]:
    merged = _merge_events(otel_events, native_events)
    timed: list[tuple[dict[str, Any], int, int | None, int | None]] = []
    for index, event in enumerate(merged):
        start, end = _timing(event)
        timed.append((event, index, start, end))
    known = [item[2] for item in timed if item[2] is not None]
    origin = min(known) if known else None
    timed.sort(key=lambda item: (item[2] is None, item[2] or 0, item[1]))
    latest = max((item[3] or item[2] or 0) for item in timed) if timed else 0
    total_ms = max(0.0, (latest - origin) / 1_000_000) if origin is not None else 0.0
    if total_ms <= 0 and timed:
        total_ms = max(1.0, float(len(timed)))
    views: list[dict[str, Any]] = []
    for row_index, (event, _, start, end) in enumerate(timed, start=1):
        if start is None or origin is None:
            offset_ms = total_ms
        else:
            offset_ms = max(0.0, (start - origin) / 1_000_000)
        duration_ms = None
        if start is not None and end is not None and end >= start:
            duration_ms = (end - start) / 1_000_000
        attr_duration = _number(_attributes(event).get("duration_ms"))
        if attr_duration is not None and attr_duration >= 0:
            duration_ms = attr_duration
        signal = _signal(event)
        status, status_class = _status(event)
        category, category_label = _trace_category(event, signal)
        bar_left = min(98.0, max(0.0, offset_ms / total_ms * 100)) if total_ms else 0.0
        bar_width = min(42.0, max(1.2, (duration_ms or 0) / total_ms * 100)) if total_ms else 2.0
        views.append(
            {
                "index": row_index,
                "signal": signal,
                "signal_label": _source_label(signal),
                "kind": str(event.get("kind") or "unknown"),
                "kind_label": _kind_label(event),
                "operation": _operation(event),
                "detail": _detail(event),
                "status": status,
                "status_class": status_class,
                "category": category,
                "category_label": category_label,
                "offset_label": _format_offset(offset_ms),
                "duration_label": _format_duration(duration_ms),
                "bar_left": f"{bar_left:.2f}",
                "bar_width": f"{bar_width:.2f}",
                "attributes": _safe_attributes(event),
                "sequence": _attributes(event).get("event.sequence"),
                "is_span": signal == "traces",
                **_span_context(event),
            }
        )
    signal_counts = Counter(_signal(event) for event in otel_events)
    trace_count = signal_counts.get("traces", 0)
    if trace_count:
        note = f"已收到 {trace_count} 个真实 OTel trace span；按时间和耗时展开，属性可继续查看。"
    elif otel_events:
        note = "当前 Run 未收到真实 trace span；以下按 OTel 日志 / 指标展示，不把 event 或 metric 冒充 span。"
    else:
        note = "当前 Run 没有 OTel 事件；以下仅展示 Agent 原生证据中可归一化的事件。"
    return {
        "events": views,
        "otel_event_count": len(otel_events),
        "native_event_count": sum(1 for event in native_events if str(event.get("kind") or "unknown") != "unknown"),
        "signal_counts": dict(signal_counts),
        "trace_count": trace_count,
        "has_trace_spans": bool(trace_count),
        "total_duration_ms": total_ms,
        "total_duration_label": _format_duration(total_ms) if total_ms else "未知",
        "axis_ticks": _axis_ticks(total_ms),
        "note": note,
    }


def _metric_value(event: dict[str, Any]) -> float | None:
    record = _record(event)
    for key in ("asInt", "asDouble", "count", "sum"):
        value = _number(record.get(key))
        if value is not None:
            return value
    summary = str(event.get("summary") or "")
    if "=" in summary:
        return _number(summary.rsplit("=", 1)[1])
    return None


def _number_label(value: Any, fallback: str = "未知") -> str:
    if value is None:
        return fallback
    number = _number(value)
    if number is None:
        return str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def build_telemetry_overview(
    telemetry: dict[str, Any],
    otel_events: list[dict[str, Any]],
    trace_view: dict[str, Any],
    native_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    otel = telemetry.get("otel") if isinstance(telemetry.get("otel"), dict) else telemetry
    native_events = native_events or []
    snapshot = build_metric_snapshot(telemetry, otel_events, native_events, trace_view)
    input_tokens = snapshot.get("input_tokens")
    output_tokens = snapshot.get("output_tokens")
    total_tokens = snapshot.get("total_tokens")
    cache_read = snapshot.get("cache_read_tokens")
    cache_creation = snapshot.get("cache_creation_tokens")
    cost = snapshot.get("cost_usd")
    models = snapshot.get("models") or []
    tool_errors = snapshot.get("tool_errors")
    signal_counts = trace_view.get("signal_counts", {})
    native_event_count = trace_view.get("native_event_count", len(native_events))
    value_source = "OTel / Agent 原生用量" if not otel_events else "OTel token 用量"
    cards = [
        {"label": "Model 调用", "value": _number_label(snapshot.get("model_calls")), "detail": "由可观察 Model 事件归纳"},
        {"label": "工具调用", "value": _number_label(snapshot.get("tool_calls")), "detail": "由可观察工具事件归纳"},
        {"label": "输入 tokens", "value": _number_label(input_tokens), "detail": value_source},
        {"label": "输出 tokens", "value": _number_label(output_tokens), "detail": value_source},
        {"label": "总 tokens", "value": _number_label(total_tokens), "detail": "输入 + 输出，不含 cache"},
        {"label": "缓存读取", "value": _number_label(cache_read), "detail": "cacheRead / Agent 原生用量"},
        {"label": "缓存创建", "value": _number_label(cache_creation), "detail": "cacheCreation / Agent 原生用量"},
        {"label": "工具错误", "value": _number_label(tool_errors), "detail": "由 tool_result 失败状态归纳"},
        {"label": "成本", "value": f"{cost:.4f} USD" if cost is not None else "未知", "detail": "仅在 Agent 提供 cost 时显示"},
        {"label": "活跃时长", "value": _format_duration(otel.get("duration_ms")), "detail": "OTel 耗时"},
        {"label": "Model", "value": ", ".join(models) or "未知", "detail": "来自实际事件属性"},
        {
            "label": "关联记录",
            "value": f"{len(otel_events)} 条" if otel_events else f"{native_event_count} 条",
            "detail": "按 ael.run.id 关联 OTel" if otel_events else "Agent 原生记录",
        },
    ]
    if otel_events:
        signals = [
            {"key": "logs", "label": "OTel 日志", "value": signal_counts.get("logs", 0), "detail": "结构化行为事件"},
            {"key": "metrics", "label": "OTel 指标", "value": signal_counts.get("metrics", 0), "detail": "token / cost / code edit"},
            {"key": "traces", "label": "OTel trace/span", "value": signal_counts.get("traces", 0), "detail": "真实 span"},
        ]
    else:
        signals = [
            {
                "key": "native",
                "label": "Agent 原生记录",
                "value": trace_view.get("native_event_count", 0),
                "detail": "没有 OTel 时使用的行为证据",
            }
        ]
    return {
        "cards": cards,
        "signals": signals,
        "models": models,
        "cache_creation": _number_label(cache_creation),
        "evidence": otel.get("evidence") or "insufficient evidence",
        "record_batches": otel.get("records") or {},
    }


def _metric_total_or_none(
    otel_events: list[dict[str, Any]],
    name_part: str,
    attr_key: str,
    attr_value: str,
) -> float | None:
    values: list[float] = []
    for event in otel_events:
        if name_part not in _event_name(event):
            continue
        if str(_attributes(event).get(attr_key)) != attr_value:
            continue
        value = _metric_value(event)
        if value is not None:
            values.append(value)
    return sum(values) if values else None


def _metric_count(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def build_metric_snapshot(
    telemetry: dict[str, Any],
    otel_events: list[dict[str, Any]],
    native_events: list[dict[str, Any]],
    trace_view: dict[str, Any],
) -> dict[str, Any]:
    """Return comparable, evidence-backed numbers for one Run.

    OTel summary values are preferred when present. Native usage/events are only
    used as a fallback, and missing values remain ``None`` so the UI can render
    them as unknown instead of implying a measurement that was not observed.
    """
    otel = telemetry.get("otel") if isinstance(telemetry.get("otel"), dict) else {}
    native_usage = telemetry.get("native_usage") if isinstance(telemetry.get("native_usage"), dict) else {}
    source_events = otel_events or native_events

    input_tokens = otel.get("input_tokens")
    if input_tokens is None:
        input_tokens = native_usage.get("input_tokens")
    output_tokens = otel.get("output_tokens")
    if output_tokens is None:
        output_tokens = native_usage.get("output_tokens")

    cache_read = _metric_total_or_none(otel_events, "token.usage", "type", "cacheRead")
    if cache_read is None:
        cache_read = native_usage.get("cache_read_input_tokens")
    cache_creation = _metric_total_or_none(otel_events, "token.usage", "type", "cacheCreation")
    if cache_creation is None:
        cache_creation = native_usage.get("cache_creation_input_tokens")

    cost_values = [
        value
        for value in (_number(_attributes(event).get("cost_usd")) for event in otel_events)
        if value is not None
    ]
    cost_usd = sum(cost_values) if cost_values else None

    model_calls = _metric_count(otel.get("model_calls"))
    if model_calls is None and otel_events:
        observed_model_events = [event for event in otel_events if _event_name(event) == "api_request"]
        model_calls = len(observed_model_events) if observed_model_events else None
    if model_calls is None and native_events:
        observed_model_events = [
            event
            for event in native_events
            if str(event.get("kind") or "") == "message" and event.get("name") == "api_request"
        ]
        model_calls = len(observed_model_events) if observed_model_events else None

    tool_calls = _metric_count(otel.get("tool_calls"))
    if tool_calls is None and source_events:
        observed_tool_events = [event for event in source_events if str(event.get("kind") or "") == "tool_call"]
        tool_calls = len(observed_tool_events) if observed_tool_events else None

    tool_result_events = [event for event in source_events if str(event.get("kind") or "") == "tool_result"]
    tool_errors = (
        sum(1 for event in tool_result_events if str(_attributes(event).get("success")).lower() == "false")
        if tool_result_events
        else None
    )
    models = sorted(
        {
            str(_attributes(event).get("model"))
            for event in otel_events
            if _attributes(event).get("model")
        }
    )
    if not models and otel.get("model"):
        models = [str(otel["model"])]

    total_tokens = None
    input_number = _number(input_tokens)
    output_number = _number(output_tokens)
    if input_number is not None and output_number is not None:
        total_tokens = input_number + output_number

    signal_counts = trace_view.get("signal_counts", {})
    return {
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors if source_events else None,
        "input_tokens": _number(input_tokens),
        "output_tokens": _number(output_tokens),
        "cache_read_tokens": _number(cache_read),
        "cache_creation_tokens": _number(cache_creation),
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "otel_duration_ms": _number(otel.get("duration_ms")),
        "models": models,
        "otel_events": len(otel_events) if otel_events else None,
        "otel_logs": signal_counts.get("logs", 0) if otel_events else None,
        "otel_metrics": signal_counts.get("metrics", 0) if otel_events else None,
        "otel_spans": signal_counts.get("traces", 0) if otel_events else None,
        "native_events": trace_view.get("native_event_count") if native_events else None,
        "evidence": otel.get("evidence") if otel_events else None,
    }


def build_otel_status(
    run: dict[str, Any],
    telemetry: dict[str, Any] | None,
    otel_events: list[dict[str, Any]],
    trace_view: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Describe the OTel boundary without turning absence into an error."""
    fingerprint = run.get("fingerprint") if isinstance(run.get("fingerprint"), dict) else {}
    agent_id = str(fingerprint.get("agent_id") or run.get("variant_id") or "unknown")
    if otel_events:
        signals = Counter(_signal(event) for event in otel_events)
        signal_text = "、".join(
            f"{label} {signals[key]} 条"
            for key, label in (("logs", "日志"), ("metrics", "指标"), ("traces", "trace/span"))
            if signals.get(key)
        )
        return {
            "state": "observed",
            "label": "已接收 OTel",
            "detail": f"Collector 已按 ael.run.id 关联 {len(otel_events)} 条记录（{signal_text or '未知 signal'}）。",
            "class": "pill-success",
        }
    if agent_id in {"codex", "pi", "hermes", "custom-harness"}:
        return {
            "state": "not_supported",
            "label": "Agent 未提供 OTel",
            "detail": f"{agent_id} 当前没有可用的 OTel 输出；本页使用 Agent 原生记录和用量证据，不伪造 OTel。",
            "class": "pill-warning",
        }
    return {
        "state": "missing",
        "label": "本次 Run 未收到 OTel",
        "detail": "AEL 已准备 OTel 关联，但 Collector 没有收到属于本次 Run 的记录；请检查 Agent 配置与 Collector。",
        "class": "pill-warning",
    }


def build_file_activity(
    native_events: list[dict[str, Any]],
    otel_events: list[dict[str, Any]],
    changed_files: list[str] | None = None,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Summarise observable per-file C/R/U/D operations.

    A tool result is counted once, and a file_change lifecycle pair is deduped
    by its item id. Workspace changes are only a labelled fallback when no
    explicit file operation was observed.
    """
    changed_files = [str(path) for path in (changed_files or [])]
    counters: dict[str, Counter[str]] = {}
    sources: dict[str, set[str]] = {}
    seen_file_changes: set[tuple[str, str, str]] = set()

    def add(path: str, operation: str, source: str) -> None:
        label = _file_label(path, changed_files)
        if not label or operation not in {"create", "read", "update", "delete"}:
            return
        counters.setdefault(label, Counter())[operation] += 1
        sources.setdefault(label, set()).add(source)

    def process(events: list[dict[str, Any]], source: str) -> None:
        pending_call: dict[str, Any] | None = None
        for event in events:
            if source == "native" and event.get("source") not in {None, "native"}:
                continue
            kind = str(event.get("kind") or "unknown")
            if kind == "tool_call":
                pending_call = {
                    "tool": _native_tool_name(event),
                    "paths": _event_file_paths(event),
                }
                continue
            if kind == "tool_result":
                result_paths = _event_file_paths(event)
                call = pending_call or {"tool": _native_tool_name(event), "paths": []}
                paths = result_paths or list(call.get("paths") or [])
                operation = _tool_operation(str(call.get("tool") or ""))
                for path in paths:
                    if operation:
                        add(path, operation, source)
                pending_call = None
                continue
            if kind == "file_change":
                item = _data(event).get("item")
                item = item if isinstance(item, dict) else {}
                item_id = str(item.get("id") or event.get("id") or "")
                changes = item.get("changes") if isinstance(item.get("changes"), list) else []
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    path = str(change.get("path") or "").strip()
                    operation = _tool_operation("", str(change.get("kind") or "update"))
                    key = (item_id or path, path, operation or "unknown")
                    if key in seen_file_changes:
                        continue
                    seen_file_changes.add(key)
                    if path and operation:
                        add(path, operation, source)
                continue
            if pending_call and kind not in {"unknown", "message"}:
                operation = _tool_operation(str(pending_call.get("tool") or ""))
                if operation:
                    for path in pending_call.get("paths") or []:
                        add(path, operation, source)
                pending_call = None
        if pending_call:
            operation = _tool_operation(str(pending_call.get("tool") or ""))
            if operation:
                for path in pending_call.get("paths") or []:
                    add(path, operation, source)

    process(native_events, "native")
    process(otel_events, "OTel")

    fallback_labels = set(counters)
    for path in changed_files:
        label = _file_label(path, changed_files)
        if label in fallback_labels:
            continue
        counters[label] = Counter(update=1)
        sources[label] = {"Workspace"}

    rows: list[dict[str, Any]] = []
    for path in sorted(counters):
        counter = counters[path]
        total = sum(counter.values())
        rows.append(
            {
                "path": path,
                "create": counter.get("create", 0),
                "read": counter.get("read", 0),
                "update": counter.get("update", 0),
                "delete": counter.get("delete", 0),
                "total": total,
                "source": "、".join(sorted(sources.get(path, set()))) or "未知",
                "observed": "Workspace" not in sources.get(path, set()),
            }
        )
    observed_operation_count = sum(row["total"] for row in rows if row["observed"])
    return {
        "rows": rows,
        "file_count": len(rows),
        "operation_count": sum(row["total"] for row in rows),
        "observed_operation_count": observed_operation_count,
        "note": "C/R/U/D 只统计可观察的 Agent 文件事件；没有明确文件事件时，Workspace 变更仅作为 U×1 的标记并单独注明。",
        "run_id": run_id,
    }


def build_evidence_sources(
    run: dict[str, Any],
    verifier: dict[str, Any],
    changed_files: list[str],
    telemetry: dict[str, Any],
    trace_view: dict[str, Any],
    workspace_observed: bool | None = None,
) -> list[dict[str, Any]]:
    signal_counts = trace_view.get("signal_counts", {})
    verifier_outcome = verifier.get("outcome") or run.get("task_outcome") or "unknown"
    return [
        {
            "label": "Verifier",
            "value": verifier_outcome,
            "status": "observed" if verifier_outcome != "unknown" else "unknown",
            "detail": "任务真值：由 Case verifier 决定 PASS / FAIL。",
        },
        {
            "label": "Workspace",
            "value": f"{len(changed_files)} 个有效变更",
            "status": "observed" if (workspace_observed if workspace_observed is not None else bool(changed_files)) else "unknown",
            "detail": "环境真值：只列出过滤 cache 后的变更文件。",
        },
        {
            "label": "OTel 日志",
            "value": f"{signal_counts.get('logs', 0)} 条",
            "status": "observed" if signal_counts.get("logs", 0) else "unknown",
            "detail": "行为证据：tool / model / lifecycle log event。",
        },
        {
            "label": "OTel 指标",
            "value": f"{signal_counts.get('metrics', 0)} 条",
            "status": "observed" if signal_counts.get("metrics", 0) else "unknown",
            "detail": "行为证据：token / cost / code edit metric。",
        },
        {
            "label": "OTel trace/span",
            "value": f"{signal_counts.get('traces', 0)} 个 span" if signal_counts.get("traces", 0) else "未收到真实 span",
            "status": "observed" if signal_counts.get("traces", 0) else "unknown",
            "detail": "没有真实 span 时保持未知，不从 logs / metrics 推断 trace 层级。",
        },
        {
            "label": "Agent 原生证据",
            "value": f"{trace_view.get('native_event_count', 0)} 条可读事件",
            "status": "observed" if trace_view.get("native_event_count") else "unknown",
            "detail": "Agent 原生事件；可能与 OTel tool event 重复，AEL 视图会避免重复堆叠。",
        },
    ]


def _trajectory_step(event: dict[str, Any], related: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    related = related or [event]
    kind = str(event.get("kind") or "unknown")
    attrs = _attributes(event)
    tool = _tool_name(event)
    tool_key = tool.lower().replace("-", "_")
    name = _event_name(event)
    text = f"{name} {event.get('summary') or ''}".lower()
    if kind in {"tool_call", "tool_result"}:
        if tool_key in _READ_TOOLS:
            group, group_label = "READ", "读取 / 搜索"
            label = f"读取 · {tool}"
        elif tool_key in _MUTATE_TOOLS:
            group, group_label = "MUTATE", "修改"
            label = f"修改 · {tool}"
        else:
            group, group_label = "TOOL", "工具"
            label = f"工具 · {tool}"
        success = next((_attributes(item).get("success") for item in related if "success" in _attributes(item)), None)
        status = "成功" if str(success).lower() == "true" else "失败" if str(success).lower() == "false" else "已观测"
        detail = f"{tool}：已看到 tool_call / tool_result" if len(related) > 1 else f"{tool}：只看到 {kind}，配对事件不足"
        duration = next((_number(_attributes(item).get("duration_ms")) for item in related if _attributes(item).get("duration_ms") is not None), None)
    elif kind == "file_change":
        group, group_label, label, detail, status, duration = "MUTATE", "修改", "代码变更", "OTel code_edit decision 已观测", "成功", None
    elif kind == "command":
        if any(token in text for token in ("pytest", "test", "verify", "check")):
            group, group_label, label = "VERIFY", "验证", "验证 · 命令"
        elif any(token in text for token in ("cat ", "sed ", "head ", "tail ", "rg ", "grep ", "find ", "ls ", "pwd")):
            group, group_label, label = "READ", "读取 / 搜索", "读取 · 命令"
        else:
            group, group_label, label = "TOOL", "工具", "工具 · 命令"
        detail, status, duration = "命令内容已观察，但按 observation profile（观测配置）脱敏", "已观测", None
    elif kind == "verification":
        group, group_label, label, detail, status, duration = "VERIFY", "验证", "验证 · 结果", str(event.get("summary") or "verifier 结果"), "已观测", None
    elif kind == "final":
        group, group_label, label, detail, status, duration = "COMPLETE", "完成", "Agent 完成", "Agent 原生完成事件", "已观测", None
    else:
        return {}
    source = _source_label(_signal(event))
    sequences = [str(_attributes(item).get("event.sequence")) for item in related if _attributes(item).get("event.sequence") is not None]
    return {
        "group": group,
        "group_label": group_label,
        "label": label,
        "detail": detail,
        "status": status,
        "status_class": "success" if status == "成功" else "observed",
        "source": source,
        "event_count": len(related),
        "duration_label": _format_duration(duration) if duration is not None else "—",
        "sequence": " / ".join(sequences) if sequences else "—",
    }


def _is_observed_completion(event: dict[str, Any]) -> bool:
    if str(event.get("kind") or "unknown") != "final" or _signal(event) != "native":
        return False
    event_type = str(_data(event).get("type") or event.get("name") or "").lower()
    return event_type in {"result", "turn.completed", "run.completed", "final", "completed"}


def build_trajectory(
    otel_events: list[dict[str, Any]],
    native_events: list[dict[str, Any]],
    verifier: dict[str, Any] | None = None,
    changed_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    events = _merge_events(otel_events, native_events)
    has_mutation_tool = any(
        str(event.get("kind") or "unknown") in {"tool_call", "tool_result"}
        and _tool_name(event).lower().replace("-", "_") in _MUTATE_TOOLS
        for event in events
    )
    action_events = [
        event
        for event in events
        if (
            str(event.get("kind") or "unknown") in {"tool_call", "tool_result", "command", "verification"}
            or (
                str(event.get("kind") or "unknown") == "file_change"
                and not has_mutation_tool
            )
            or (
                str(event.get("kind") or "unknown") == "final"
                and _is_observed_completion(event)
            )
        )
    ]
    indexed_events = sorted(
        enumerate(action_events),
        key=lambda item: _trajectory_order_key(item[1], item[0]),
    )
    action_events = [event for _, event in indexed_events]
    steps: list[dict[str, Any]] = []
    index = 0
    while index < len(action_events):
        event = action_events[index]
        kind = str(event.get("kind") or "unknown")
        related = [event]
        if kind == "tool_call" and index + 1 < len(action_events):
            next_event = action_events[index + 1]
            if str(next_event.get("kind") or "unknown") == "tool_result" and _tool_name(next_event) == _tool_name(event):
                related.append(next_event)
                index += 1
        step = _trajectory_step(event, related)
        if step:
            if steps and steps[-1]["group"] == step["group"] and steps[-1]["label"] == step["label"] and step["group"] in {"READ", "TOOL"}:
                steps[-1]["event_count"] += step["event_count"]
                steps[-1]["detail"] = f"{steps[-1]['detail']}；连续重复事件已合并"
                if steps[-1]["duration_label"] == "—" and step["duration_label"] != "—":
                    steps[-1]["duration_label"] = step["duration_label"]
            else:
                step["index"] = len(steps) + 1
                steps.append(step)
        index += 1
    if changed_files and not any(step["group"] == "MUTATE" for step in steps):
        steps.insert(
            next((i for i, step in enumerate(steps) if step["group"] == "COMPLETE"), len(steps)),
            {
                "index": 0,
                "group": "MUTATE",
                "group_label": "修改",
                "label": "Workspace 变更",
                "detail": ", ".join(changed_files),
                "status": "已观测",
                "status_class": "observed",
                "source": "Workspace",
                "event_count": len(changed_files),
                "duration_label": "—",
                "sequence": "—",
            },
        )
    verifier_phases = build_verifier_phases(verifier)
    for phase in verifier_phases:
        steps.append(
            {
                "index": len(steps) + 1,
                "group": "VERIFY",
                "group_label": "验证",
                "label": f"Verifier · {phase['label']} {phase['status']}",
                "detail": phase["detail"],
                "status": phase["status"],
                "status_class": phase["status_class"],
                "source": phase["source"],
                "event_count": phase["event_count"],
                "duration_label": phase["duration_label"],
                "sequence": phase["sequence"],
                "phase": True,
            }
        )
    outcome = (verifier or {}).get("outcome")
    if outcome in {"PASS", "FAIL"}:
        steps.append(
            {
                "index": len(steps) + 1,
                "group": "VERIFY",
                "group_label": "验证",
                "label": f"Verifier · {'任务结论' if verifier_phases else '全量验证'} {outcome}",
                "detail": "任务真值；stdout / stderr 可在下方展开。",
                "status": outcome,
                "status_class": "success" if outcome == "PASS" else "failed",
                "source": "Verifier",
                "event_count": 1,
                "duration_label": _format_duration((_number((verifier or {}).get("duration_seconds")) or 0) * 1000),
                "sequence": "—",
            }
        )
    for index, step in enumerate(steps, start=1):
        step["index"] = index
    return steps


def align_trajectories(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    left_groups = [item.get("group") for item in left]
    right_groups = [item.get("group") for item in right]
    table = [[0] * (len(right_groups) + 1) for _ in range(len(left_groups) + 1)]
    for i in range(len(left_groups) - 1, -1, -1):
        for j in range(len(right_groups) - 1, -1, -1):
            if left_groups[i] == right_groups[j]:
                table[i][j] = table[i + 1][j + 1] + 1
            else:
                table[i][j] = max(table[i + 1][j], table[i][j + 1])
    pairs: list[tuple[int | None, int | None]] = []
    i = j = 0
    while i < len(left) or j < len(right):
        if i < len(left) and j < len(right) and left_groups[i] == right_groups[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif i < len(left) and (j >= len(right) or table[i + 1][j] >= table[i][j + 1]):
            pairs.append((i, None))
            i += 1
        else:
            pairs.append((None, j))
            j += 1
    rows: list[dict[str, Any]] = []
    first = True
    for left_index, right_index in pairs:
        candidate = left[left_index] if left_index is not None else None
        reference = right[right_index] if right_index is not None else None
        if candidate and reference:
            same = candidate.get("group") == reference.get("group") and candidate.get("label") == reference.get("label")
            status = "MATCH" if same else "DIVERGENCE"
        elif candidate:
            status = "CANDIDATE_ONLY"
        else:
            status = "REFERENCE_ONLY"
        meaningful = status != "MATCH" and (candidate or reference)
        boundary = any(
            str(item.get("source") or "").lower() == "verifier"
            for item in (candidate, reference)
            if item
        )
        rows.append(
            {
                "candidate": candidate,
                "reference": reference,
                "status": status,
                "status_label": {
                    "MATCH": "对齐",
                    "DIVERGENCE": "行为差异",
                    "CANDIDATE_ONLY": "仅候选",
                    "REFERENCE_ONLY": "仅 PASS 参考",
                }[status],
                "first_divergence": bool(meaningful and first),
                "boundary_divergence": bool(meaningful and boundary),
            }
        )
        if meaningful:
            first = False
    return rows
