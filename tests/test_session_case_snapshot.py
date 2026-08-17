from __future__ import annotations

from pathlib import Path

import pytest

from ael.web import _DEFAULT_SESSION_EXCLUDES, _copy_session_fixture


def test_session_fixture_snapshot_applies_default_excludes(tmp_path: Path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / ".git").mkdir()
    (source / "node_modules" / "pkg").mkdir(parents=True)
    (source / "__pycache__").mkdir()
    (source / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".git" / "config").write_text("private\n", encoding="utf-8")
    (source / "node_modules" / "pkg" / "index.js").write_text("private\n", encoding="utf-8")
    (source / "__pycache__" / "app.pyc").write_bytes(b"private")

    destination = tmp_path / "destination"
    _copy_session_fixture(source, destination, relevant_files=[], excludes=_DEFAULT_SESSION_EXCLUDES)

    assert (destination / "src" / "app.py").exists()
    assert not (destination / ".git").exists()
    assert not (destination / "node_modules").exists()
    assert not (destination / "__pycache__").exists()


def test_session_fixture_snapshot_honors_relevant_files_without_minimizer(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("keep\n", encoding="utf-8")
    (source / "drop.txt").write_text("drop\n", encoding="utf-8")
    destination = tmp_path / "destination"

    _copy_session_fixture(source, destination, relevant_files=["keep.txt"], excludes=_DEFAULT_SESSION_EXCLUDES)

    assert (destination / "keep.txt").read_text() == "keep\n"
    assert not (destination / "drop.txt").exists()


def test_session_fixture_rejects_parent_escape(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="相对路径"):
        _copy_session_fixture(source, tmp_path / "destination", relevant_files=["../secret"], excludes=set())
