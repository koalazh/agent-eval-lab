from __future__ import annotations

from fastapi.testclient import TestClient

from ael.web import create_app


def test_server_rendered_navigation_on_empty_repository(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.get("/").status_code == 200
    assert client.get("/agents").status_code == 200
    assert client.get("/cases").status_code == 200
    assert client.get("/experiments").status_code == 200
    assert client.get("/failures").status_code == 200

