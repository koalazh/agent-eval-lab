from __future__ import annotations

from pathlib import Path

import pytest

from ael.cases import CaseSpec, ExperimentSpec, SuiteSpec, VerifierSpec
from ael.models import AgentVariant, ObservationProfile, RunMode
from ael.runner import Runner


def make_experiment(root: Path, agent_id: str, behavior: str = "pass", trials: int = 1) -> ExperimentSpec:
    fixture = root / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "answer.txt").write_text("wrong\n", encoding="utf-8")
    case = CaseSpec(
        id="case-001",
        prompt="make the answer pass",
        fixture_path=fixture,
        verifier=VerifierSpec(command='test "$(cat answer.txt)" = "pass"'),
        timeout_seconds=1,
    )
    variant = AgentVariant(
        id=agent_id,
        agent_id=agent_id,
        model="test-model",
        provider="test",
        harness_config={"fake_behavior": behavior},
        run_mode=RunMode.CONTROLLED,
        observation_profile=ObservationProfile.MINIMAL,
    )
    return ExperimentSpec(
        id=f"experiment-{agent_id}",
        suite=SuiteSpec("development", "development", (case,)),
        variants=(variant,),
        trials=trials,
        max_concurrency=2,
    )


def test_python_verifier_content_changes_case_revision(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "answer.txt").write_text("wrong\n", encoding="utf-8")
    grader = tmp_path / "grader.py"
    grader.write_text("print('v1')\n", encoding="utf-8")
    case_kwargs = dict(
        id="python-case",
        prompt="check it",
        fixture_path=fixture,
        verifier=VerifierSpec(python="grader.py"),
        source_path=tmp_path / "case.yaml",
    )
    first = CaseSpec(**case_kwargs)
    grader.write_text("print('v2')\n", encoding="utf-8")
    second = CaseSpec(**case_kwargs)
    assert first.revision != second.revision


def test_case_revision_ignores_generated_python_cache(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    case_kwargs = dict(
        id="stable-case",
        prompt="check it",
        fixture_path=fixture,
        verifier=VerifierSpec(command="true"),
        source_path=tmp_path / "case.yaml",
    )
    first = CaseSpec(**case_kwargs)
    cache = fixture / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-313.pyc").write_bytes(b"generated cache")
    second = CaseSpec(**case_kwargs)
    assert first.revision == second.revision


@pytest.mark.asyncio
async def test_m0_case_run_verify_persist(fake_runner, repo, tmp_path):
    experiment = make_experiment(tmp_path, "fake-pass", "pass")
    result = (await fake_runner.run_experiment(experiment))[0]
    assert result["status"] == "COMPLETED"
    assert result["outcome"] == "PASS"
    assert result["changes"]["changed_files"] == ["answer.txt"]
    persisted = repo.get_run(result["run_id"])
    assert persisted["run_status"] == "COMPLETED"
    assert persisted["task_outcome"] == "PASS"
    evidence = Path(result["evidence_dir"])
    assert (evidence / "metadata.json").exists()
    assert (evidence / "native" / "events.jsonl").read_text()
    assert (evidence / "workspace" / "diff.patch").exists()
    assert (evidence / "verifier" / "result.json").exists()
    assert not list((repo.ael_dir / "workspaces").iterdir())


def test_execution_workspace_is_outside_repository(repo):
    from ael.workspace import WorkspaceManager

    fixture = repo.root / "fixture"
    fixture.mkdir()
    (fixture / "file.txt").write_text("fixture\n", encoding="utf-8")
    manager = WorkspaceManager(repo.ael_dir)
    workspace, _ = manager.create(fixture, "workspace-boundary")
    try:
        assert repo.root not in workspace.parents
    finally:
        manager.cleanup(workspace)


@pytest.mark.asyncio
async def test_verifier_failure_is_completed_process_but_task_fail(fake_runner, repo, tmp_path):
    experiment = make_experiment(tmp_path, "fake-fail", "fail")
    result = (await fake_runner.run_experiment(experiment))[0]
    assert result["status"] == "COMPLETED"
    assert result["outcome"] == "FAIL"


@pytest.mark.asyncio
async def test_timeout_and_crash_are_not_task_failures(fake_runner, repo, tmp_path):
    timeout_exp = make_experiment(tmp_path / "timeout", "fake-timeout", "timeout")
    timeout_result = (await fake_runner.run_experiment(timeout_exp))[0]
    assert timeout_result["status"] == "TIMEOUT"
    assert timeout_result["outcome"] == "UNKNOWN"

    crash_exp = make_experiment(tmp_path / "crash", "fake-crash", "crash")
    crash_result = (await fake_runner.run_experiment(crash_exp))[0]
    assert crash_result["status"] == "PROCESS_ERROR"
    assert crash_result["outcome"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_trials_are_isolated_and_flaky_is_observable(fake_runner, repo, tmp_path):
    experiment = make_experiment(tmp_path / "flaky", "fake-flaky", "flaky", trials=3)
    results = await fake_runner.run_experiment(experiment)
    assert [item["outcome"] for item in results] == ["PASS", "FAIL", "PASS"]
    assert len({item["run_id"] for item in results}) == 3
