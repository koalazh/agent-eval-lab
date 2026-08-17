from __future__ import annotations

from ael.agents import builtin_real_drivers, collector_status
from ael.drivers.real import ClaudeCodeDriver, CodexDriver
from ael.reports import matrix_report, trial_summary


def test_trial_classification_is_explicit():
    assert trial_summary(["PASS", "PASS", "PASS"])["classification"] == "STABLE_PASS"
    assert trial_summary(["FAIL", "FAIL", "FAIL"])["classification"] == "STABLE_FAIL"
    assert trial_summary(["PASS", "FAIL", "PASS"])["classification"] == "FLAKY"
    assert trial_summary(["UNKNOWN", "UNKNOWN"])["classification"] == "ERROR"
    assert trial_summary(["FAIL", "UNKNOWN"])["display"] == "0/2 PASS · 1 次未知"
    assert trial_summary(["PASS", "UNKNOWN"])["display"] == "1/2 PASS · 1 次未知"


def test_matrix_report_groups_case_variant_trials():
    report = matrix_report(
        [
            {"id": "pass-id", "case_id": "case", "variant_id": "a", "task_outcome": "PASS"},
            {"id": "fail-id", "case_id": "case", "variant_id": "a", "task_outcome": "FAIL"},
            {"id": "other-id", "case_id": "case", "variant_id": "b", "task_outcome": "PASS"},
        ]
    )
    assert report["runs"] == 3
    assert report["rows"][0]["classification"] == "FLAKY"
    assert report["matrix_rows"][0]["cells"]["a"]["target_run_id"] == "fail-id"


def test_matrix_report_accepts_runner_summaries():
    report = matrix_report(
        [{"case_id": "case", "variant_id": "a", "outcome": "PASS"}]
    )
    assert report["rows"][0]["classification"] == "STABLE_PASS"


def test_real_registry_probe_is_read_only_and_reports_current_path():
    drivers = builtin_real_drivers()
    assert set(drivers) == {"codex", "claude-code", "pi", "hermes", "generic-cli"}
    for driver in drivers.values():
        capabilities = driver.probe()
        assert isinstance(capabilities.available, bool)
        assert driver.agent().detected_version
    assert collector_status()["host"] == "127.0.0.1"


def test_codex_nested_command_event_is_normalized_to_command():
    event = CodexDriver().normalize_native_event(
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "pytest -q"},
        }
    )
    assert event.kind == "command"
    assert event.summary == "pytest -q"


def test_claude_assistant_tool_use_is_normalized_to_tool_call():
    event = ClaudeCodeDriver().normalize_native_event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
        }
    )
    assert event.kind == "tool_call"
    assert event.name == "Bash"
