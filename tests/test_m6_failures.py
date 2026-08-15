from __future__ import annotations

import pytest

from ael.cases import ExperimentSpec, SuiteSpec, load_experiment
from ael.drivers.fake import FakeAgentDriver
from ael.failures import build_regression_experiment, promote_failure
from ael.models import AgentVariant
from ael.persistence import Repository
from ael.runner import Runner
from tests.test_m5_diagnosis import write_experiment


@pytest.mark.asyncio
async def test_only_valid_task_failures_enter_failure_book(tmp_path):
    repo = Repository(tmp_path)
    experiment = load_experiment(write_experiment(tmp_path))
    runner = Runner(
        repo,
        {
            "fake-fail": FakeAgentDriver("fake-fail", "fail"),
            "fake-crash": FakeAgentDriver("fake-crash", "crash"),
        },
    )
    failed = (await runner.run_experiment(experiment))[0]
    assert failed["failure_id"].startswith("failure-")
    assert len(repo.list_failures()) == 1

    crash_experiment = load_experiment(write_experiment(tmp_path / "crash"))
    crash_experiment = crash_experiment.__class__(
        id="crash-exp",
        suite=crash_experiment.suite,
        variants=(AgentVariant(id="fake-crash", agent_id="fake-crash", model="test"),),
        trials=1,
        max_concurrency=1,
        source_path=crash_experiment.source_path,
    )
    crashed = (await runner.run_experiment(crash_experiment))[0]
    assert crashed["failure_id"] is None
    assert len(repo.list_failures()) == 1


@pytest.mark.asyncio
async def test_promote_pins_case_revision_and_regression_suite_can_rerun(tmp_path):
    repo = Repository(tmp_path)
    experiment = load_experiment(write_experiment(tmp_path))
    runner = Runner(repo, {"fake-fail": FakeAgentDriver("fake-fail", "fail")})
    failed = (await runner.run_experiment(experiment))[0]
    failure = repo.list_failures()[0]

    promoted = promote_failure(repo, failure["id"])
    assert promoted.id == "case-1"
    assert promoted.fixture_path.exists()
    assert (tmp_path / "cases" / "case-1" / "fixture" / "answer.txt").read_text() == "wrong\n"
    assert not (tmp_path / "cases" / "regression").exists()
    assert repo.get_failure(failure["id"])["status"] == "REGRESSION_GUARDED"
    assert repo.suite_cases("regression")[0].id == promoted.id
    assert repo.suite_cases("regression")[0].revision == experiment.suite.cases[0].revision

    regression_experiment = build_regression_experiment(
        repo,
        experiment_id="regression-rerun",
        variants=(
            AgentVariant(
                id="fake-pass",
                agent_id="fake-pass",
                model="test",
                harness_config={"fake_behavior": "pass"},
            ),
        ),
        trials=1,
    )
    rerun = Runner(repo, {"fake-pass": FakeAgentDriver("fake-pass", "pass")})
    result = (await rerun.run_experiment(regression_experiment))[0]
    assert result["case_id"] == promoted.id
    assert result["outcome"] == "PASS"


@pytest.mark.asyncio
async def test_promotion_rejects_changed_source_fixture(tmp_path):
    repo = Repository(tmp_path)
    experiment = load_experiment(write_experiment(tmp_path))
    run = (await Runner(repo, {"fake-fail": FakeAgentDriver("fake-fail", "fail")}).run_experiment(experiment))[0]
    source_fixture = experiment.suite.cases[0].fixture_path / "answer.txt"
    source_fixture.write_text("changed after run\n", encoding="utf-8")
    with pytest.raises(ValueError, match="已不再匹配"):
        promote_failure(repo, run["failure_id"])


@pytest.mark.asyncio
async def test_repeated_verifier_failure_is_clustered_and_reproduced(tmp_path):
    repo = Repository(tmp_path)
    experiment = load_experiment(write_experiment(tmp_path))
    experiment = experiment.__class__(
        id="repeat-failures",
        suite=experiment.suite,
        variants=experiment.variants,
        trials=2,
        max_concurrency=1,
        source_path=experiment.source_path,
    )
    results = await Runner(repo, {"fake-fail": FakeAgentDriver("fake-fail", "fail")}).run_experiment(experiment)
    assert len(results) == 2
    failures = repo.list_failures()
    assert len(failures) == 1
    assert failures[0]["status"] == "REPRODUCED"
    assert failures[0]["details"]["run_count"] == 2


@pytest.mark.asyncio
async def test_follow_up_requires_discriminating_baseline_and_candidate(tmp_path):
    repo = Repository(tmp_path)
    source_experiment = load_experiment(write_experiment(tmp_path))
    runner = Runner(repo, {"fake-fail": FakeAgentDriver("fake-fail", "fail"), "fake-pass": FakeAgentDriver("fake-pass", "pass")})
    source_run = (await runner.run_experiment(source_experiment))[0]

    candidate_only = ExperimentSpec(
        id="candidate-only",
        suite=source_experiment.suite,
        variants=(AgentVariant(id="candidate-only", agent_id="fake-pass", model="test", harness_config={"fake_behavior": "pass"}),),
        metadata={
            "follow_up_of_run": source_run["run_id"],
            "baseline_variant_id": "missing-baseline",
            "candidate_variant_id": "candidate-only",
        },
    )
    await runner.run_experiment(candidate_only)
    assert repo.get_failure(source_run["failure_id"])["status"] == "OBSERVED"

    discriminating = ExperimentSpec(
        id="discriminating-follow-up",
        suite=SuiteSpec(source_experiment.suite.id, source_experiment.suite.kind, source_experiment.suite.cases),
        variants=(
            AgentVariant(id="baseline", agent_id="fake-fail", model="test", harness_config={"fake_behavior": "fail"}),
            AgentVariant(id="candidate", agent_id="fake-pass", model="test", harness_config={"fake_behavior": "pass"}),
        ),
        metadata={
            "follow_up_of_run": source_run["run_id"],
            "baseline_variant_id": "baseline",
            "candidate_variant_id": "candidate",
        },
    )
    await runner.run_experiment(discriminating)

    failure = repo.get_failure(source_run["failure_id"])
    assert failure["status"] == "FIXED"
    assert failure["details"]["baseline_outcomes"] == ["FAIL"]
    assert failure["details"]["candidate_outcomes"] == ["PASS"]
    assert failure["details"]["discriminating_experiment"] is True
