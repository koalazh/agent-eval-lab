from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ael.persistence import Repository
from ael.web import _build_experiment, _failure_rollups, create_app


def test_server_rendered_navigation_on_empty_repository(tmp_path):
    client = TestClient(create_app(tmp_path))
    home = client.get("/")
    assert home.status_code == 200
    assert "新建实验" in home.text
    assert "New Experiment" not in home.text
    builder = client.get("/experiments/new")
    assert builder.status_code == 200
    assert "运行实验" in builder.text
    assert "Run experiment" not in builder.text
    assert client.get("/agents").status_code == 200
    assert client.get("/cases").status_code == 200
    assert client.get("/experiments").status_code == 200
    assert client.get("/failures").status_code == 200


def test_builder_rejects_case_definition_outside_repository(tmp_path: Path):
    case_dir = tmp_path / "examples" / "cases" / "inside"
    fixture_dir = case_dir / "fixture"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "answer.txt").write_text("ok\n", encoding="utf-8")
    (case_dir / "case.yaml").write_text(
        """
id: inside
prompt: make the answer pass
fixture:
  path: fixture
verify:
  command: test "$(cat answer.txt)" = ok
""",
        encoding="utf-8",
    )
    outside = tmp_path.parent / "outside-case.yaml"
    outside.write_text(
        """
id: outside
prompt: should never run
fixture:
  path: .
verify:
  command: true
""",
        encoding="utf-8",
    )

    rows = [{"agent": {"id": "codex"}, "capabilities": {"available": True, "supports_models": True}}]
    repository = Repository(tmp_path)
    try:
        _build_experiment(
            repository,
            rows,
            {"case_path": [str(outside)], "agent_id": ["codex"]},
        )
    except ValueError as exc:
        assert "当前工作区已注册" in str(exc)
    else:
        raise AssertionError("builder accepted a case outside the repository")


def test_failure_page_rolls_up_legacy_per_run_rows():
    rows = [
        {
            "id": "failure-a",
            "source_run_id": "run-a",
            "status": "OBSERVED",
            "created_at": "2026-01-01",
            "details": {
                "case_id": "premature-completion",
                "case_revision": "rev-1",
                "variant_id": "codex-default",
                "run_ids": ["run-a"],
            },
        },
        {
            "id": "failure-b",
            "source_run_id": "run-b",
            "status": "OBSERVED",
            "created_at": "2026-01-02",
            "details": {
                "case_id": "premature-completion",
                "case_revision": "rev-1",
                "variant_id": "claude-default",
                "run_ids": ["run-b"],
            },
        },
    ]

    rollups = _failure_rollups(rows)

    assert len(rollups) == 1
    assert rollups[0]["status"] == "REPRODUCED"
    assert rollups[0]["details"]["run_count"] == 2
    assert rollups[0]["details"]["variant_ids"] == ["claude-default", "codex-default"]
    assert rollups[0]["rollup_failure_ids"] == ["failure-a", "failure-b"]
