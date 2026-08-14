from __future__ import annotations

import pytest

from ael.comparison import compare_experiments, compare_run_details, first_meaningful_divergence
from ael.runner import Runner
from tests.test_m0_core import make_experiment


@pytest.mark.asyncio
async def test_failure_explorer_matches_same_revision_pass_and_localizes_evidence(fake_runner, repo, tmp_path):
    failed = make_experiment(tmp_path / "failed", "fake-fail", "fail")
    passed = make_experiment(tmp_path / "passed", "fake-pass", "pass")
    failed_run = (await fake_runner.run_experiment(failed))[0]
    await fake_runner.run_experiment(passed)

    details = compare_run_details(repo, failed_run["run_id"])
    assert details["matched_reference"]["outcome"] == "PASS"
    assert details["variable_scope"]["same"]
    assert details["artifact_diff"]["diff"]
    assert details["first_meaningful_divergence"]["reason"] == "verifier outcome differs"
    assert details["first_meaningful_divergence"]["candidate"]["label"] == "FULL VERIFY FAIL"
    assert "因果" in details["note"]
    assert {row["label"] for row in details["metric_rows"]} >= {"端到端时长", "总 tokens", "工具调用", "OTel 证据"}


@pytest.mark.asyncio
async def test_compare_experiments_reports_fixed_and_scope(fake_runner, repo, tmp_path):
    baseline = make_experiment(tmp_path / "baseline", "fake-fail", "fail", trials=3)
    candidate = make_experiment(tmp_path / "candidate", "fake-pass", "pass", trials=3)
    await fake_runner.run_experiment(baseline)
    await fake_runner.run_experiment(candidate)

    report = compare_experiments(repo, baseline.id, candidate.id)
    row = report["differential_cases"][0]
    assert row["label"] == "FIXED"
    assert row["confidence"] in {"CONTROLLED", "PARTIAL", "DESCRIPTIVE"}
    assert row["scope"]["same"]


@pytest.mark.asyncio
async def test_no_reference_is_explicit(fake_runner, repo, tmp_path):
    failed = make_experiment(tmp_path, "fake-fail", "fail")
    failed_run = (await fake_runner.run_experiment(failed))[0]
    details = compare_run_details(repo, failed_run["run_id"])
    assert details["matched_reference"] is None
    assert details["timeline_diff"]["status"] == "INSUFFICIENT_REFERENCE"
    assert "没有足够" in details["note"]


def test_action_groups_align_verification_anchors_without_zip_drift():
    candidate = [
        {"kind": "command", "summary": "pytest -q -k targeted"},
        {"kind": "final", "summary": "complete"},
    ]
    reference = [
        {"kind": "command", "summary": "pytest -q -k targeted"},
        {"kind": "command", "summary": "pytest -q"},
        {"kind": "final", "summary": "complete"},
    ]

    divergence = first_meaningful_divergence(candidate, reference)

    assert divergence["status"] == "DIVERGENCE"
    assert divergence["candidate"] is None
    assert divergence["reference"]["label"] == "VERIFY full"
