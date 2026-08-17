from __future__ import annotations

import json
from pathlib import Path

import pytest

from ael.models import ObservationProfile
from ael.observation import filter_jsonl
from ael.otel_ingest import ingest_collector_output
from ael.otel_lifecycle import AELLifecycle
from ael.redaction import redact_text


def test_minimal_and_telemetry_omit_sensitive_structured_payloads():
    raw = json.dumps(
        {
            "type": "tool_call",
            "prompt": "private prompt",
            "arguments": {"command": "cat secret.txt"},
            "summary": "inspect file",
        }
    )
    filtered = filter_jsonl(raw, ObservationProfile.MINIMAL)
    assert "private prompt" not in filtered
    assert "cat secret.txt" not in filtered
    assert "inspect file" in filtered


def test_deep_preserves_structured_payloads():
    raw = json.dumps({"prompt": "private prompt", "arguments": {"x": 1}})
    filtered = filter_jsonl(raw, ObservationProfile.DEEP)
    assert "private prompt" in filtered
    assert "arguments" in filtered


def test_redaction_covers_common_credentials():
    text = "api_key=sk-1234567890abcdef and Authorization: Bearer abcdefghijklmnop"
    result = redact_text(text)
    assert "sk-1234567890abcdef" not in result
    assert "abcdefghijklmnop" not in result
    assert "[REDACTED]" in result


def test_collector_output_is_correlated_by_run_id(tmp_path: Path):
    collector = tmp_path / ".ael" / "otel"
    collector.mkdir(parents=True)
    payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "ael.run.id", "value": {"stringValue": "run-1"}},
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1",
                                "body": {"stringValue": "tool_call Bash"},
                                "attributes": [
                                    {"key": "event.name", "value": {"stringValue": "tool_call"}},
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    (collector / "logs.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    events, summary = ingest_collector_output(tmp_path, "run-1", tmp_path / "run")

    assert len(events) == 1
    assert events[0].source == "otel"
    assert events[0].kind == "tool_call"
    assert summary["records"] == {"logs": 1}
    assert summary["evidence"].startswith("real OTLP")


def test_otel_ingest_does_not_classify_free_text_as_an_action(tmp_path: Path):
    collector = tmp_path / ".ael" / "otel"
    collector.mkdir(parents=True)
    payload = {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "ael.run.id", "value": {"stringValue": "run-1"}}]},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {"body": {"stringValue": "edit the file and run bash"}, "timeUnixNano": "1"}
                        ]
                    }
                ],
            }
        ]
    }
    (collector / "logs.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    events, _ = ingest_collector_output(tmp_path, "run-1", tmp_path / "run")

    assert events[0].kind == "unknown"


def test_ael_lifecycle_sdk_spans_keep_real_parent_relationships():
    lifecycle = AELLifecycle(endpoint=None, resource={"ael.run.id": "run-1"})
    root = lifecycle.start("ael.run")
    child = lifecycle.start("agent.execute", parent_span_id=root)
    lifecycle.end(child, attributes={"ael.run.status": "COMPLETED"})
    lifecycle.end(root, attributes={"ael.task.outcome": "PASS"})

    spans = lifecycle.span_snapshot()
    export = lifecycle.export()
    assert len(spans) == 2
    assert spans[1]["parent_span_id"] == spans[0]["span_id"]
    assert spans[0]["trace_id"] == spans[1]["trace_id"]
    assert export["exported"] is False
    assert export["endpoint_configured"] is False


@pytest.mark.asyncio
async def test_run_persists_profile_and_evidence(fake_runner, repo, tmp_path):
    from tests.test_m0_core import make_experiment

    experiment = make_experiment(tmp_path, "fake-jsonl", "jsonl")
    result = (await fake_runner.run_experiment(experiment))[0]
    evidence = Path(result["evidence_dir"])
    metadata = json.loads((evidence / "metadata.json").read_text())
    assert metadata["fingerprint"]["observation_profile"] == "minimal"
    assert (evidence / "native" / "raw.jsonl").exists()
    assert (evidence / "telemetry" / "raw" / "events.jsonl").exists()
    assert (evidence / "telemetry" / "summary.json").exists()
    lifecycle = json.loads((evidence / "telemetry" / "otel" / "ael-lifecycle.json").read_text())
    assert lifecycle["export"]["span_count"] == 5
    summary = json.loads((evidence / "telemetry" / "summary.json").read_text())
    assert summary["otel"]["ael_lifecycle"]["span_count"] == 5


@pytest.mark.asyncio
async def test_normalized_event_payload_respects_profile(repo, tmp_path):
    from ael.drivers.fake import FakeAgentDriver
    from ael.models import ObservableEvent
    from ael.runner import Runner
    from tests.test_m0_core import make_experiment

    class SensitiveFake(FakeAgentDriver):
        async def execute(self, context, supervisor):
            result = await super().execute(context, supervisor)
            result.native_events.append(
                ObservableEvent(
                    "tool_call",
                    name="bash",
                    summary="inspect",
                    data={"prompt": "private", "arguments": {"command": "cat private"}},
                )
            )
            return result

    runner = Runner(repo, {"fake-jsonl": SensitiveFake("fake-jsonl", "jsonl")})
    experiment = make_experiment(tmp_path, "fake-jsonl", "jsonl")
    result = (await runner.run_experiment(experiment))[0]
    events = (Path(result["evidence_dir"]) / "native" / "events.jsonl").read_text()
    assert "private" not in events
