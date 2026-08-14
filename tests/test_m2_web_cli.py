from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ael.persistence import Repository
from ael.web import _build_experiment, create_app


def test_server_rendered_navigation_on_empty_repository(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.get("/").status_code == 200
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
