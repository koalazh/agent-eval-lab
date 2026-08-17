from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ael.cases import CaseSpec, ExperimentSpec, SuiteSpec, VerifierSpec, load_case
from ael.comparison import build_experiment_comparison, compare_run_details
from ael.drivers.generic_cli import GenericCLIDriver
from ael.models import AgentVariant
from ael.persistence import Repository
from ael.runner import Runner
from ael.web import create_app


HARNESS = Path(__file__).parents[1] / "examples" / "harnesses" / "generic_cli_harness.py"
MINIMAL_V1 = Path(__file__).parents[1] / "examples" / "harnesses" / "minimal-v1"
MINIMAL_V2 = Path(__file__).parents[1] / "examples" / "harnesses" / "minimal-v2"


def _case(root: Path, *, answer: str = "wrong") -> CaseSpec:
    fixture = root / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "answer.txt").write_text(f"{answer}\n", encoding="utf-8")
    return CaseSpec(
        id="generic-case",
        prompt="make answer pass",
        fixture_path=fixture,
        verifier=VerifierSpec(command='test "$(cat answer.txt)" = "pass"'),
        timeout_seconds=5,
        source_path=root / "case.yaml",
    )


def _variant(identifier: str, *arguments: str) -> AgentVariant:
    return AgentVariant(
        id=identifier,
        agent_id="generic-cli",
        name=identifier,
        executable=str(HARNESS),
        subject_revision="harness-v1",
        agent_version="configured",
        arguments=arguments,
        prompt_transport="stdin",
    )


@pytest.mark.asyncio
async def test_two_persistent_minimal_harness_versions_survive_restart(tmp_path):
    client = TestClient(create_app(tmp_path))
    common = {
        "agent_id": "generic-cli",
        "agent_version": "configured",
        "model": "default",
        "provider": "default",
        "run_mode": "native",
        "observation_profile": "minimal",
        "model_config": "{}",
        "harness_config": "{}",
        "arguments": "[]",
        "prompt_transport": "stdin",
        "env_delta": "{}",
        "version_command": "[]",
    }
    for name, executable, revision in (
        ("Minimal Harness v1", MINIMAL_V1, "minimal-v1"),
        ("Minimal Harness v2", MINIMAL_V2, "minimal-v2"),
    ):
        response = client.post(
            "/variants/new",
            data={
                **common,
                "name": name,
                "executable": str(executable),
                "subject_revision": revision,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    # Reconstruct the app and repository to model an AEL restart.
    restarted_client = TestClient(create_app(tmp_path))
    assert "Minimal Harness v1" in restarted_client.get("/variants").text
    restarted = Repository(tmp_path)
    rows = restarted.list_variants()
    v1 = AgentVariant.from_dict(next(row for row in rows if row["subject_revision"] == "minimal-v1"))
    v2 = AgentVariant.from_dict(next(row for row in rows if row["subject_revision"] == "minimal-v2"))
    assert v1.executable == str(MINIMAL_V1)
    assert v2.executable == str(MINIMAL_V2)

    case = _case(tmp_path / "case")
    experiment = ExperimentSpec(
        id="persistent-minimal-versions",
        suite=SuiteSpec("generic", "coding", (case,)),
        variants=(
            v1,
            v2,
        ),
        metadata={"baseline_variant_id": v1.id, "candidate_variant_id": v2.id},
    )
    runs = await Runner(restarted, {"generic-cli": GenericCLIDriver()}).run_experiment(experiment)

    assert [run["outcome"] for run in runs] == ["FAIL", "PASS"]
    assert runs[0]["execution_receipt"]["resolved_executable"] == str(MINIMAL_V1)
    assert runs[1]["execution_receipt"]["resolved_executable"] == str(MINIMAL_V2)


@pytest.mark.asyncio
async def test_generic_cli_receipt_drives_fixed_decision_and_effective_change(tmp_path):
    repo = Repository(tmp_path)
    case = _case(tmp_path / "case")
    experiment = ExperimentSpec(
        id="generic-fixed",
        suite=SuiteSpec("generic", "coding", (case,)),
        variants=(_variant("baseline", "--result=wrong"), _variant("candidate", "--result=pass")),
        metadata={"baseline_variant_id": "baseline", "candidate_variant_id": "candidate"},
    )
    runs = await Runner(repo, {"generic-cli": GenericCLIDriver()}).run_experiment(experiment)

    assert [run["outcome"] for run in runs] == ["FAIL", "PASS"]
    assert runs[0]["execution_receipt"]["execution_source"] == "driver"
    assert runs[0]["execution_receipt"]["argv"][-1] == "--result=wrong"
    assert runs[0]["fingerprint"]["effective_execution_hash"] != runs[1]["fingerprint"]["effective_execution_hash"]
    comparison = build_experiment_comparison(
        repo,
        repo.list_runs(experiment.id),
        repo.read_experiment_definition(experiment.id)["variants"],
        definition=repo.read_experiment_definition(experiment.id),
    )
    assert comparison["decision_matrix"]["counts"]["FIXED"] == 1
    assert comparison["effective_execution"]["status"] == "CHANGED"

    details = compare_run_details(repo, runs[1]["run_id"], reference_run_id=runs[0]["run_id"])
    assert details["effective_execution"]["status"] == "CHANGED"
    assert details["effective_execution"]["candidate_receipt"]["prompt_hash"] == case.prompt_hash
    assert repo.get_variant("baseline") is None
    with pytest.raises(ValueError, match="definition 已冻结"):
        repo.save_experiment(
            replace(
                experiment,
                variants=(_variant("baseline", "--result=pass"), _variant("candidate", "--result=pass")),
            )
        )


@pytest.mark.asyncio
async def test_same_persistent_cli_inputs_are_explicitly_no_effective_change(tmp_path):
    repo = Repository(tmp_path)
    case = _case(tmp_path / "case")
    experiment = ExperimentSpec(
        id="generic-unchanged",
        suite=SuiteSpec("generic", "coding", (case,)),
        variants=(_variant("baseline", "--result=pass"), _variant("candidate", "--result=pass")),
        metadata={"baseline_variant_id": "baseline", "candidate_variant_id": "candidate"},
    )
    await Runner(repo, {"generic-cli": GenericCLIDriver()}).run_experiment(experiment)
    definition = repo.read_experiment_definition(experiment.id)
    comparison = build_experiment_comparison(repo, repo.list_runs(experiment.id), definition["variants"], definition=definition)
    assert comparison["effective_execution"]["label"] == "NO EFFECTIVE CHANGE"


@pytest.mark.asyncio
async def test_case_revision_r1_remains_runnable_after_authoring_moves_to_r2(tmp_path):
    case_dir = tmp_path / "cases" / "generic-case"
    fixture = case_dir / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "answer.txt").write_text("wrong\n", encoding="utf-8")
    case_path = case_dir / "case.yaml"
    case_path.write_text(
        "id: generic-case\nprompt: make answer pass\nfixture:\n  path: fixture\n"
        "verify:\n  command: 'test \"$(cat answer.txt)\" = \"pass\"'\n",
        encoding="utf-8",
    )
    r1 = load_case(case_path)
    r1_experiment = ExperimentSpec(
        id="generic-r1",
        suite=SuiteSpec("generic", "coding", (r1,)),
        variants=(_variant("no-write", "--no-write"),),
    )
    runner = Runner(repo := Repository(tmp_path), {"generic-cli": GenericCLIDriver()})
    first = (await runner.run_experiment(r1_experiment))[0]
    assert first["outcome"] == "FAIL"

    (fixture / "answer.txt").write_text("pass\n", encoding="utf-8")
    r2 = load_case(case_path)
    assert r2.revision != r1.revision
    old_again = (await Runner(Repository(tmp_path), {"generic-cli": GenericCLIDriver()}).run_experiment(r1_experiment))[0]
    assert old_again["outcome"] == "FAIL"
    new_experiment = ExperimentSpec(
        id="generic-r2",
        suite=SuiteSpec("generic", "coding", (r2,)),
        variants=(_variant("no-write-r2", "--no-write"),),
    )
    new_run = (await runner.run_experiment(new_experiment))[0]
    assert new_run["outcome"] == "PASS"
    assert repo.get_case("generic-case", r1.revision).fixture_path != repo.get_case("generic-case", r2.revision).fixture_path


def test_case_revision_cannot_be_silently_overwritten(repo):
    case = _case(repo.root / "case")
    repo.save_case(case)
    conflicting = CaseSpec(
        id=case.id,
        prompt="different prompt",
        fixture_path=case.fixture_path,
        verifier=case.verifier,
        timeout_seconds=case.timeout_seconds,
        constraints=case.constraints,
        source_path=case.source_path,
        revision=case.revision,
    )
    with pytest.raises(ValueError, match="CaseRevision 已冻结"):
        repo.save_case(conflicting)


@pytest.mark.asyncio
async def test_next_experiment_enters_normal_builder_with_duplicate_variant(tmp_path):
    repo = Repository(tmp_path)
    case = _case(tmp_path / "case")
    source = _variant("persistent-source", "--result=pass")
    repo.save_variant(source)
    experiment = ExperimentSpec(
        id="next-entry",
        suite=SuiteSpec("generic", "coding", (case,)),
        variants=(source,),
    )
    run = (await Runner(repo, {"generic-cli": GenericCLIDriver()}).run_experiment(experiment))[0]

    client = TestClient(create_app(tmp_path))
    response = client.get(f"/runs/{run['run_id']}/next-experiment", follow_redirects=False)
    assert response.status_code == 303
    assert "source_run_id=" + run["run_id"] in response.headers["location"]
    assert len(repo.list_variants()) == 2
    builder = client.get(response.headers["location"])
    assert builder.status_code == 200
    assert "进入正常 Experiment Builder" not in builder.text
    assert "persistent-source · next candidate" in builder.text
