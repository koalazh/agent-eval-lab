from __future__ import annotations

from ael.trace_view import (
    align_trajectories,
    build_evidence_sources,
    build_metric_snapshot,
    build_file_activity,
    build_otel_status,
    build_telemetry_overview,
    build_trace_view,
    build_trajectory,
)


def _otel_event(
    *,
    signal: str,
    kind: str,
    name: str,
    timestamp: str,
    attributes: dict[str, object] | None = None,
    record: dict[str, object] | None = None,
):
    return {
        "kind": kind,
        "name": name,
        "summary": name,
        "source": "otel",
        "timestamp": timestamp,
        "data": {
            "signal": signal,
            "attributes": attributes or {},
            "record": record or {"timeUnixNano": timestamp},
        },
    }


def test_trace_view_keeps_signal_boundaries_and_does_not_fake_spans():
    events = [
        _otel_event(
            signal="metrics",
            kind="message",
            name="claude_code.token.usage",
            timestamp="2000000000",
            attributes={"type": "input"},
            record={"timeUnixNano": "2000000000", "asInt": 12},
        ),
        _otel_event(
            signal="logs",
            kind="tool_call",
            name="tool_decision",
            timestamp="1000000000",
            attributes={"tool_name": "Read", "decision": "accept", "event.sequence": "1"},
        ),
        _otel_event(
            signal="logs",
            kind="tool_result",
            name="tool_result",
            timestamp="1100000000",
            attributes={"tool_name": "Read", "success": "true", "duration_ms": "100", "event.sequence": "2"},
        ),
    ]

    view = build_trace_view(events, [])

    assert view["signal_counts"] == {"metrics": 1, "logs": 2}
    assert view["trace_count"] == 0
    assert view["has_trace_spans"] is False
    assert view["view_mode"] == "event_timeline"
    assert view["span_relationship_count"] == 0
    assert "未收到真实 trace span" in view["note"]
    assert len(view["axis_ticks"]) == 5
    assert view["events"][0]["operation"] == "调用 Read"
    assert view["events"][1]["duration_label"] == "100 ms"


def test_trace_view_exposes_real_span_context_only_for_trace_signal():
    event = _otel_event(
        signal="traces",
        kind="message",
        name="agent.run",
        timestamp="1000000000",
        record={
            "startTimeUnixNano": "1000000000",
            "endTimeUnixNano": "1200000000",
            "traceId": "trace-1",
            "spanId": "span-1",
            "parentSpanId": "parent-1",
            "kind": "SPAN_KIND_INTERNAL",
            "events": [{"name": "tool_call"}],
        },
    )
    view = build_trace_view([event], [])

    assert view["has_trace_spans"] is True
    assert view["view_mode"] == "waterfall"
    assert view["events"][0]["operation"] == "agent.run"
    assert view["events"][0]["trace_id"] == "trace-1"
    assert view["events"][0]["span_id"] == "span-1"
    assert view["events"][0]["parent_span_id"] == "parent-1"
    assert view["events"][0]["span_event_count"] == 1


def test_trace_waterfall_indentation_uses_only_observed_parent_span_ids():
    parent = _otel_event(
        signal="traces",
        kind="message",
        name="ael.run",
        timestamp="1000000000",
        record={"startTimeUnixNano": "1000000000", "endTimeUnixNano": "1300000000", "spanId": "parent"},
    )
    child = _otel_event(
        signal="traces",
        kind="message",
        name="agent.execute",
        timestamp="1100000000",
        record={
            "startTimeUnixNano": "1100000000",
            "endTimeUnixNano": "1200000000",
            "spanId": "child",
            "parentSpanId": "parent",
        },
    )

    view = build_trace_view([parent, child], [])

    assert view["span_relationship_count"] == 1
    assert {event["span_id"]: event["span_depth"] for event in view["events"]} == {"parent": 0, "child": 1}


def test_trace_view_keeps_tool_call_before_result_when_duration_is_inferred():
    events = [
        _otel_event(
            signal="logs",
            kind="tool_call",
            name="tool_decision",
            timestamp="1000000000",
            attributes={"tool_name": "Bash", "event.sequence": "1"},
        ),
        _otel_event(
            signal="logs",
            kind="tool_result",
            name="tool_result",
            timestamp="1055000000",
            attributes={"tool_name": "Bash", "success": "true", "duration_ms": "55", "event.sequence": "2"},
        ),
    ]

    view = build_trace_view(events, [])

    assert [event["operation"] for event in view["events"]] == ["调用 Bash", "Bash 返回"]


def test_trajectory_orders_otlp_signals_and_pairs_tool_events():
    events = [
        _otel_event(
            signal="metrics",
            kind="file_change",
            name="claude_code.code_edit_tool.decision",
            timestamp="3000000000",
            attributes={"tool_name": "Edit", "decision": "accept"},
        ),
        _otel_event(
            signal="logs",
            kind="tool_call",
            name="tool_decision",
            timestamp="1000000000",
            attributes={"tool_name": "Read", "event.sequence": "1"},
        ),
        _otel_event(
            signal="logs",
            kind="tool_result",
            name="tool_result",
            timestamp="1100000000",
            attributes={"tool_name": "Read", "success": "true", "event.sequence": "2"},
        ),
    ]

    steps = build_trajectory(events, [], verifier={"outcome": "FAIL", "duration_seconds": 0.01})

    assert [step["group"] for step in steps] == ["READ", "MUTATE", "VERIFY"]
    assert steps[0]["event_count"] == 2
    assert steps[-1]["label"] == "Verifier · 全量验证 FAIL"


def test_otel_message_records_do_not_look_like_agent_completion():
    event = _otel_event(
        signal="logs",
        kind="final",
        name="api_request",
        timestamp="1000000000",
        attributes={"model": "model-x", "duration_ms": "120"},
    )

    view = build_trace_view([event], [])
    steps = build_trajectory([event], [])

    assert view["events"][0]["operation"] == "Model 调用"
    assert view["events"][0]["category"] == "model"
    assert view["events"][0]["kind_label"] == "Model 调用"
    assert steps == []


def test_verifier_phases_keep_checkpoint_evidence_separate_from_agent_trajectory():
    from ael.trace_view import build_verifier_phases

    phases = build_verifier_phases(
        {
            "outcome": "FAIL",
            "stdout": "targeted verification: 1 passed, 1 deselected in 0.00s\nvisible full suite: 2 passed in 0.00s",
            "stderr": "RuntimeError: pagination cursor did not advance from None while the server reported more pages",
        }
    )

    assert [phase["label"] for phase in phases] == ["定向验证", "可见全量测试", "最终边界检查"]
    assert phases[-1]["status"] == "FAIL"
    assert "pagination cursor" in phases[-1]["detail"]


def test_metric_snapshot_keeps_telemetry_fields_comparable_and_unknown_explicit():
    events = [
        _otel_event(
            signal="logs",
            kind="message",
            name="api_request",
            timestamp="1000000000",
            attributes={"model": "model-x", "input_tokens": 100, "output_tokens": 25},
        ),
        _otel_event(
            signal="logs",
            kind="tool_call",
            name="tool_decision",
            timestamp="1100000000",
            attributes={"tool_name": "Bash"},
        ),
        _otel_event(
            signal="metrics",
            kind="message",
            name="claude_code.token.usage",
            timestamp="1200000000",
            attributes={"type": "cacheRead"},
            record={"timeUnixNano": "1200000000", "asInt": 40},
        ),
    ]
    telemetry = {"otel": {"model_calls": 1, "tool_calls": 1, "input_tokens": 100, "output_tokens": 25}}
    view = build_trace_view(events, [])
    metrics = build_metric_snapshot(telemetry, events, [], view)

    assert metrics["total_tokens"] == 125
    assert metrics["cache_read_tokens"] == 40
    assert metrics["models"] == ["model-x"]
    assert metrics["cost_usd"] is None
    assert metrics["otel_spans"] == 0


def test_telemetry_overview_keeps_missing_cache_metrics_unknown():
    overview = build_telemetry_overview(
        {"otel": {"input_tokens": 10, "output_tokens": 2}},
        [],
        {"signal_counts": {}},
    )
    cards = {card["label"]: card["value"] for card in overview["cards"]}

    assert cards["缓存读取"] == "未知"
    assert cards["缓存创建"] == "未知"


def test_native_overview_does_not_present_zero_otel_signals_as_evidence():
    native = [{"source": "native", "kind": "command", "name": "pytest", "timestamp": "1000000000"}]
    view = build_trace_view([], native)
    overview = build_telemetry_overview({"native_usage": {"input_tokens": 4, "output_tokens": 2}}, [], view, native)
    status = build_otel_status({"fingerprint": {"agent_id": "codex"}}, {}, [])

    assert overview["signals"] == [
        {"key": "native", "label": "Agent 原生记录", "value": 1, "detail": "没有 OTel 时使用的行为证据"}
    ]
    assert overview["cards"][-1]["label"] == "关联记录"
    assert overview["cards"][-1]["value"] == "1 条"
    assert overview["cards"][-1]["detail"] == "Agent 原生记录"
    assert status["label"] == "Agent 未提供 OTel"


def test_evidence_sources_distinguish_ael_lifecycle_from_vendor_trace():
    sources = build_evidence_sources(
        {"task_outcome": "PASS"},
        {"outcome": "PASS"},
        [],
        {
            "otel": {
                "ael_lifecycle": {"exported": True, "span_count": 5},
            }
        },
        {"signal_counts": {"traces": 2}},
    )

    lifecycle = next(source for source in sources if source["label"] == "AEL lifecycle OTel")
    vendor_trace = next(source for source in sources if source["label"] == "OTel trace/span")
    assert lifecycle["value"] == "5 个 span"
    assert lifecycle["status"] == "observed"
    assert vendor_trace["value"] == "2 个 span"


def test_trajectory_alignment_marks_first_different_action():
    left = [
        {"group": "READ", "label": "读取 · Read"},
        {"group": "COMPLETE", "label": "Agent 完成"},
    ]
    right = [
        {"group": "READ", "label": "读取 · Read"},
        {"group": "VERIFY", "label": "验证 · 全量"},
        {"group": "COMPLETE", "label": "Agent 完成"},
    ]

    rows = align_trajectories(left, right)

    assert rows[1]["first_divergence"] is True
    assert rows[1]["status"] == "REFERENCE_ONLY"


def test_file_activity_counts_explicit_native_operations_once():
    native = [
        {"source": "native", "kind": "tool_call", "name": "Read", "data": {}},
        {
            "source": "native",
            "kind": "tool_result",
            "name": "tool",
            "data": {"tool_use_result": {"file": {"filePath": "/tmp/run/paginator.py"}}},
        },
        {"source": "native", "kind": "tool_call", "name": "Edit", "data": {}},
        {
            "source": "native",
            "kind": "tool_result",
            "name": "tool",
            "data": {"tool_use_result": {"filePath": "/tmp/run/paginator.py"}},
        },
        {
            "source": "native",
            "kind": "file_change",
            "data": {"item": {"id": "change-1", "changes": [{"kind": "update", "path": "/tmp/run/other.py"}]}},
        },
        {
            "source": "native",
            "kind": "file_change",
            "data": {"item": {"id": "change-1", "changes": [{"kind": "update", "path": "/tmp/run/other.py"}]}},
        },
    ]

    activity = build_file_activity(native, [], ["paginator.py", "other.py"])

    rows = {row["path"]: row for row in activity["rows"]}
    assert rows["paginator.py"]["read"] == 1
    assert rows["paginator.py"]["update"] == 1
    assert rows["other.py"]["update"] == 1
    assert activity["operation_count"] == 3


def test_file_activity_marks_workspace_fallback_as_not_observed():
    activity = build_file_activity([], [], ["changed.py"])

    row = activity["rows"][0]
    assert row["path"] == "changed.py"
    assert row["update"] == 1
    assert row["observed"] is False


def test_native_turn_started_is_not_presented_as_agent_completion():
    events = [
        {"source": "native", "kind": "final", "data": {"type": "turn.started"}},
        {"source": "native", "kind": "command", "name": "pytest", "data": {}},
        {"source": "native", "kind": "final", "data": {"type": "turn.completed"}},
    ]

    steps = build_trajectory(events, [])
    view = build_trace_view([], events)

    assert [step["group"] for step in steps] == ["VERIFY", "COMPLETE"]
    assert view["events"][0]["operation"] == "回合开始"
