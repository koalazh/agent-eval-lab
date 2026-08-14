from __future__ import annotations

from ael.agents import builtin_real_drivers, collector_status
from ael.reports import matrix_report, trial_summary


def test_trial_classification_is_explicit():
    assert trial_summary(["PASS", "PASS", "PASS"])["classification"] == "STABLE_PASS"
    assert trial_summary(["FAIL", "FAIL", "FAIL"])["classification"] == "STABLE_FAIL"
    assert trial_summary(["PASS", "FAIL", "PASS"])["classification"] == "FLAKY"
    assert trial_summary(["UNKNOWN", "UNKNOWN"])["classification"] == "ERROR"


def test_matrix_report_groups_case_variant_trials():
    report = matrix_report(
        [
            {"case_id": "case", "variant_id": "a", "task_outcome": "PASS"},
            {"case_id": "case", "variant_id": "a", "task_outcome": "FAIL"},
            {"case_id": "case", "variant_id": "b", "task_outcome": "PASS"},
        ]
    )
    assert report["runs"] == 3
    assert report["rows"][0]["classification"] == "FLAKY"


def test_real_registry_probe_is_read_only_and_reports_current_path():
    drivers = builtin_real_drivers()
    assert set(drivers) == {"codex", "claude-code", "pi", "hermes"}
    for driver in drivers.values():
        capabilities = driver.probe()
        assert isinstance(capabilities.available, bool)
        assert driver.agent().detected_version
    assert collector_status()["host"] == "127.0.0.1"

