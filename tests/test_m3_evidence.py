from __future__ import annotations

import json
from pathlib import Path

import pytest

from ael.models import ObservationProfile
from ael.observation import filter_jsonl
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
