from __future__ import annotations

from pathlib import Path

import pytest

from ael.cases import load_experiment
from ael.diagnosis import build_diagnosis_packet, create_follow_up_experiment, diagnose_run
from ael.drivers.fake import FakeAgentDriver
from ael.persistence import Repository
from ael.runner import Runner


def write_experiment(root: Path) -> Path:
    case_dir = root / "cases" / "case-1"
    fixture = case_dir / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "answer.txt").write_text("wrong\n", encoding="utf-8")
    (case_dir / "case.yaml").write_text(
        "id: case-1\nfixture:\n  path: fixture\nprompt: fix it\n"
        "verify:\n  command: 'test \"$(cat answer.txt)\" = \"pass\"'\n"
        "limits:\n  timeout_seconds: 2\n",
        encoding="utf-8",
    )
    experiment = root / "experiment.yaml"
    experiment.write_text(
        "id: diagnosis-exp\nsuite: development\ncases:\n  - cases/case-1/case.yaml\n"
        "variants:\n  - id: fake-fail\n    agent: fake-fail\n    model: test\n    config:\n      fake_behavior: fail\n"
        "trials: 1\nmax_concurrency: 1\n",
        encoding="utf-8",
    )
    return experiment


@pytest.mark.asyncio
async def test_diagnosis_is_structured_and_followup_is_draft(tmp_path):
    repo = Repository(tmp_path)
    experiment = load_experiment(write_experiment(tmp_path))
    runner = Runner(repo, {"fake-fail": FakeAgentDriver("fake-fail", "fail")})
    run = (await runner.run_experiment(experiment))[0]

    packet = build_diagnosis_packet(repo, run["run_id"])
    assert set(
        ["observed", "hypotheses", "evidence", "counter_evidence", "unknowns", "suggested_improvement", "best_next_experiment"]
    ) <= packet.keys()
    assert packet["model_assisted"] is False
    assert packet["best_next_experiment"]["requires_user_confirmation"] is True

    original_revision = repo.get_run(run["run_id"])["case_revision"]
    (experiment.suite.cases[0].fixture_path / "answer.txt").write_text("fixture changed\n", encoding="utf-8")
    follow_up = create_follow_up_experiment(repo, run["run_id"])
    assert follow_up.id.startswith("follow-up-diagnosis-exp-")
    assert follow_up.suite.cases[0].revision == original_revision
    stored = next(item for item in repo.list_experiments() if item["id"] == follow_up.id)
    assert stored["status"] == "DRAFT"
    assert stored["follow_up_of"] == run["run_id"]


def test_diagnosis_without_reference_is_explicit(tmp_path):
    repo = Repository(tmp_path)
    assert diagnose_run(repo, "missing") == {}
