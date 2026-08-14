from __future__ import annotations

from ael.trace_view import align_trajectories, build_trace_view, build_trajectory


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
    assert "未收到真实 trace span" in view["note"]
    assert len(view["axis_ticks"]) == 5
    assert view["events"][0]["operation"] == "调用 Read"
    assert view["events"][1]["duration_label"] == "100 ms"


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
    assert steps == []


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
