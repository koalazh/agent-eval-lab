from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ael.persistence import Repository
from ael.web import create_app


def _agent_rows() -> list[dict]:
    return [
        {
            "agent": {
                "id": "fake-agent",
                "display_name": "Fake Agent",
                "driver": "fake",
                "binary": "fake-agent",
                "detected_version": "0.1.0",
            },
            "capabilities": {
                "available": True,
                "supports_models": True,
                "notes": [],
            },
        }
    ]


def _case(root: Path) -> str:
    case_dir = root / "examples" / "cases" / "inside"
    fixture = case_dir / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "answer.txt").write_text("wrong\n", encoding="utf-8")
    (case_dir / "case.yaml").write_text(
        """
id: inside
prompt: make the answer pass
fixture:
  path: fixture
verify:
  command: test "$(cat answer.txt)" = pass
""",
        encoding="utf-8",
    )
    return "examples/cases/inside/case.yaml"


def test_web_variant_duplicate_edit_and_experiment_snapshot(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("ael.web.probe_registry", lambda repository: _agent_rows())
    case_path = _case(tmp_path)
    client = TestClient(create_app(tmp_path))

    created = client.post(
        "/variants/new",
        data={
            "name": "Minimal Harness A",
            "agent_id": "fake-agent",
            "executable": "fake-agent",
            "subject_revision": "git:abc123",
            "agent_version": "0.1.0",
            "model": "gpt-test",
            "provider": "local",
            "model_config": '{"temperature": 0}',
            "harness_config": '{"memory": false}',
            "run_mode": "native",
            "observation_profile": "minimal",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    variant_a_id = created.headers["location"].split("/")[2]

    duplicated = client.post(f"/variants/{variant_a_id}/duplicate", follow_redirects=False)
    assert duplicated.status_code == 303
    variant_b_id = duplicated.headers["location"].split("/")[2]
    updated = client.post(
        f"/variants/{variant_b_id}",
        data={
            "name": "Minimal Harness B",
            "agent_id": "fake-agent",
            "executable": "fake-agent",
            "subject_revision": "git:def456",
            "agent_version": "0.1.0",
            "model": "gpt-test",
            "provider": "local",
            "model_config": '{"temperature": 0}',
            "harness_config": '{"memory": true}',
            "run_mode": "native",
            "observation_profile": "minimal",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303

    repository = Repository(tmp_path)
    variant_a = repository.get_variant(variant_a_id)
    variant_b = repository.get_variant(variant_b_id)
    assert variant_a["subject_revision"] == "git:abc123"
    assert variant_b["subject_revision"] == "git:def456"

    case = next(item for item in repository.list_cases() if item["id"] == "inside") if repository.list_cases() else None
    if case is None:
        from ael.cases import load_case

        loaded = load_case(tmp_path / case_path)
        repository.save_case(loaded)
        case_revision = loaded.revision
    else:
        case_revision = case["revision"]
    experiment_response = client.post(
        "/experiments/new",
        data={
            "case_selected__inside": "1",
            "case_revision__inside": case_revision,
            "variant_id": [variant_a_id, variant_b_id],
            "baseline_variant_id": variant_a_id,
            "candidate_variant_id": variant_b_id,
            "experiment_name": "variant-snapshot",
            "suite_id": "variant-snapshot",
            "trials": "1",
            "max_concurrency": "1",
        },
        follow_redirects=False,
    )
    assert experiment_response.status_code == 303
    experiment_id = experiment_response.headers["location"].split("/")[-1]
    definition = repository.read_experiment_definition(experiment_id)
    snapshots = {item["id"]: item for item in definition["variants"]}
    assert snapshots[variant_a_id]["subject_revision"] == "git:abc123"
    assert snapshots[variant_b_id]["subject_revision"] == "git:def456"
    assert definition["metadata"]["baseline_variant_id"] == variant_a_id
    assert definition["metadata"]["candidate_variant_id"] == variant_b_id

    client.post(
        f"/variants/{variant_a_id}",
        data={
            "name": "Minimal Harness A edited",
            "agent_id": "fake-agent",
            "executable": "fake-agent",
            "subject_revision": "git:later",
            "agent_version": "0.1.0",
            "model": "gpt-test",
            "provider": "local",
            "model_config": "{}",
            "harness_config": "{}",
            "run_mode": "native",
            "observation_profile": "minimal",
        },
        follow_redirects=False,
    )
    persisted_definition = repository.read_experiment_definition(experiment_id)
    persisted_snapshots = {item["id"]: item for item in persisted_definition["variants"]}
    assert persisted_snapshots[variant_a_id]["subject_revision"] == "git:abc123"
