from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from ael.sessions import discover_sessions
from ael.persistence import Repository
from ael.web import create_app


def _resource(*, session_id: str, run_id: str | None = None) -> dict:
    attrs = [
        {"key": "service.name", "value": {"stringValue": "claude-code"}},
        {"key": "service.version", "value": {"stringValue": "2.1.229"}},
    ]
    if session_id:
        attrs.append({"key": "session.id", "value": {"stringValue": session_id}})
    if run_id:
        attrs.append({"key": "ael.run.id", "value": {"stringValue": run_id}})
    return {"attributes": attrs}


def _event_attrs(session_id: str, name: str, **extra: object) -> list[dict]:
    attrs = [
        {"key": "session.id", "value": {"stringValue": session_id}},
        {"key": "event.name", "value": {"stringValue": name}},
    ]
    for key, value in extra.items():
        value_type = "intValue" if isinstance(value, int) else "stringValue"
        attrs.append({"key": key, "value": {value_type: str(value)}})
    return attrs


def _write_collector(root: Path) -> None:
    collector = root / ".ael" / "otel"
    collector.mkdir(parents=True)
    external = "external-session-1"
    managed = "managed-session-1"
    (collector / "logs.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "resourceLogs": [
                            {
                                "resource": _resource(session_id=external),
                                "scopeLogs": [
                                    {
                                        "logRecords": [
                                            {
                                                "timeUnixNano": "1700000000000000000",
                                                "body": {"stringValue": "tool"},
                                                "attributes": _event_attrs(external, "tool_call", tool_name="Bash"),
                                            },
                                            {
                                                "timeUnixNano": "1700000001000000000",
                                                "body": {"stringValue": "model"},
                                                "attributes": _event_attrs(external, "api_request", model="test-model", input_tokens=10, output_tokens=4),
                                            },
                                        ]
                                    }
                                ],
                            }
                        ]
                    }
                ),
                json.dumps(
                    {
                        "resourceLogs": [
                            {
                                "resource": _resource(session_id=managed, run_id="run-managed"),
                                "scopeLogs": [
                                    {
                                        "logRecords": [
                                            {
                                                "timeUnixNano": "1700000002000000000",
                                                "body": {"stringValue": "managed"},
                                                "attributes": _event_attrs(managed, "tool_call"),
                                            }
                                        ]
                                    }
                                ],
                            }
                        ]
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (collector / "metrics.jsonl").write_text(
        json.dumps(
            {
                "resourceMetrics": [
                    {
                        "resource": _resource(session_id=external),
                        "scopeMetrics": [
                            {
                                "metrics": [
                                    {
                                        "name": "claude_code.token.usage",
                                        "sum": {
                                            "dataPoints": [
                                                {
                                                    "startTimeUnixNano": "1700000000000000000",
                                                    "timeUnixNano": "1700000001000000000",
                                                    "asInt": 14,
                                                    "attributes": _event_attrs(external, "token.usage", type="input"),
                                                }
                                            ]
                                        },
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (collector / "traces.jsonl").write_text(
        json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": _resource(session_id=external),
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "trace-external",
                                        "spanId": "span-external",
                                        "name": "claude.session",
                                        "startTimeUnixNano": "1700000000000000000",
                                        "endTimeUnixNano": "1700000001000000000",
                                        "attributes": _event_attrs(external, "claude.session"),
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_external_sessions_are_projected_from_session_id_without_task_outcome(tmp_path: Path):
    _write_collector(tmp_path)

    sessions = discover_sessions(tmp_path)

    assert [session["vendor_session_id"] for session in sessions] == ["external-session-1"]
    session = sessions[0]
    assert session["origin"] == "External terminal（不是 AEL 发起）"
    assert session["outcome"] == "UNVERIFIED"
    assert session["signals"] == {"logs": 2, "metrics": 1, "traces": 1}
    assert session["telemetry"]["otel"]["correlation"] == "session.id"
    assert len(session["events"]) == 4


def test_external_session_uses_the_same_evidence_page_and_boundary(tmp_path: Path):
    _write_collector(tmp_path)

    client = TestClient(create_app(tmp_path))
    sessions = client.get("/sessions")
    detail = client.get("/sessions/external-session-1")

    assert sessions.status_code == 200
    assert "external-session-1" in sessions.text
    assert "managed-session-1" in sessions.text
    assert "Managed by AEL Run run-managed" in sessions.text
    assert detail.status_code == 200
    assert "UNVERIFIED" in detail.text
    assert "session.id" in detail.text
    assert "OTel Trace Waterfall" in detail.text
    assert "Case Verifier" in detail.text


def test_real_session_can_become_a_user_confirmed_case_revision(tmp_path: Path):
    _write_collector(tmp_path)
    fixture = tmp_path / "confirmed-fixture"
    fixture.mkdir()
    (fixture / "answer.txt").write_text("expected\n", encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    form = client.get("/sessions/external-session-1/case/new")
    assert form.status_code == 200
    assert "Create CaseRevision" in form.text
    response = client.post(
        "/sessions/external-session-1/case/new",
        data={
            "source_session_id": "external-session-1",
            "case_id": "confirmed-session-case",
            "display_name": "Confirmed Session Case",
            "prompt": "Make answer.txt contain expected.",
            "fixture_source": str(fixture),
            "relevant_files": "answer.txt",
            "verifier_command": 'test "$(cat answer.txt)" = expected',
            "timeout_seconds": "30",
            "notes": "human confirmed",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    case_root = tmp_path / "examples" / "cases" / "confirmed-session-case"
    assert (case_root / "fixture" / "answer.txt").read_text() == "expected\n"
    assert "verify:" in (case_root / "case.yaml").read_text()
    assert "Confirmed Session Case" in client.get("/cases/confirmed-session-case").text
    assert "confirmed-session-case" in client.get("/experiments/new").text
    assert Repository(tmp_path).list_experiments() == []
