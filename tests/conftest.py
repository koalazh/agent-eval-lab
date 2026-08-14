from __future__ import annotations

from pathlib import Path

import pytest

from ael.drivers.fake import FakeAgentDriver
from ael.persistence import Repository
from ael.runner import Runner


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository(tmp_path)


@pytest.fixture
def fake_runner(repo: Repository) -> Runner:
    return Runner(
        repo,
        {
            "fake-pass": FakeAgentDriver("fake-pass", "pass"),
            "fake-fail": FakeAgentDriver("fake-fail", "fail"),
            "fake-timeout": FakeAgentDriver("fake-timeout", "timeout"),
            "fake-crash": FakeAgentDriver("fake-crash", "crash"),
            "fake-jsonl": FakeAgentDriver("fake-jsonl", "jsonl"),
            "fake-flaky": FakeAgentDriver("fake-flaky", "flaky"),
        },
    )

