from __future__ import annotations

import pytest

from ael.comparison import (
    build_experiment_comparison,
    compare_experiments,
    compare_run_details,
    compare_variant_snapshots,
    first_meaningful_divergence,
)
from ael.cases import ExperimentSpec
from ael.models import AgentVariant
from ael.runner import Runner
from tests.test_m0_core import make_experiment


@pytest.mark.asyncio
async def test_failure_explorer_matches_same_revision_pass_and_localizes_evidence(fake_runner, repo, tmp_path):
    failed = make_experiment(tmp_path / "failed", "fake-fail", "fail")
    passed = make_experiment(tmp_path / "passed", "fake-pass", "pass")
    failed_run = (await fake_runner.run_experiment(failed))[0]
    passed_run = (await fake_runner.run_experiment(passed))[0]

    details = compare_run_details(repo, failed_run["run_id"], reference_run_id=passed_run["run_id"])
    assert details["matched_reference"]["outcome"] == "PASS"
    assert details["variable_scope"]["same"]
    assert details["artifact_diff"]["diff"]
    assert details["first_meaningful_divergence"]["reason"] == "verifier outcome differs"
    assert details["first_meaningful_divergence"]["candidate"]["label"] == "完整验证 FAIL"
    assert "因果" in details["note"]
    assert details["reference"]["source"] == "user-selected"
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
    passed = make_experiment(tmp_path / "unrelated-pass", "fake-pass", "pass")
    await fake_runner.run_experiment(passed)
    details = compare_run_details(repo, failed_run["run_id"])
    assert details["matched_reference"] is None
    assert details["timeline_diff"]["status"] == "INSUFFICIENT_REFERENCE"
    assert "不会跨数据库猜" in details["note"]


@pytest.mark.asyncio
async def test_decision_matrix_uses_explicit_baseline_candidate_pair(fake_runner, repo, tmp_path):
    source = make_experiment(tmp_path, "fake-fail", "fail", trials=2)
    experiment = ExperimentSpec(
        id="decision-matrix",
        suite=source.suite,
        variants=(
            AgentVariant(
                id="baseline",
                agent_id="fake-fail",
                model="test-model",
                provider="test",
                subject_revision="git:baseline",
                harness_config={"fake_behavior": "fail"},
            ),
            AgentVariant(
                id="candidate",
                agent_id="fake-pass",
                model="test-model",
                provider="test",
                subject_revision="git:candidate",
                harness_config={"fake_behavior": "pass"},
            ),
        ),
        trials=2,
        max_concurrency=2,
        metadata={"baseline_variant_id": "baseline", "candidate_variant_id": "candidate"},
    )
    await fake_runner.run_experiment(experiment)

    comparison = build_experiment_comparison(
        repo,
        repo.list_runs(experiment.id),
        repo.read_experiment_definition(experiment.id)["variants"],
        definition=repo.read_experiment_definition(experiment.id),
    )
    decision = comparison["decision_matrix"]
    assert decision["counts"] == {"FIXED": 1, "REGRESSED": 0, "UNCHANGED": 0, "INCONCLUSIVE": 0}
    assert decision["rows"][0]["baseline"]["display"] == "0/2 PASS"
    assert decision["rows"][0]["candidate"]["display"] == "2/2 PASS"
    assert decision["rows"][0]["candidate_run_id"]
    assert decision["rows"][0]["reference_run_id"]


def test_variant_snapshot_comparison_is_explicit_about_changed_dimensions():
    baseline = {
        "id": "a",
        "agent_id": "harness",
        "executable": "harness",
        "agent_version": "1.0",
        "subject_revision": "git:abc",
        "model": "model-x",
        "provider": "provider-y",
        "model_config": {},
        "harness_config": {"memory": False},
        "run_mode": "native",
    }
    candidate = {**baseline, "id": "b", "subject_revision": "git:def", "harness_config": {"memory": True}}
    comparison = compare_variant_snapshots(baseline, candidate)
    assert comparison["validity"] == "DESCRIPTIVE"
    assert comparison["changed"] == ["subject revision", "Harness 配置"]
    assert comparison["same"]


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
