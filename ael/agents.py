from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from .drivers.base import AgentDriver
from .drivers.fake import FakeAgentDriver
from .drivers.generic_cli import GenericCLIDriver
from .drivers.real import ClaudeCodeDriver, CodexDriver, HermesDriver, PiDriver
from .persistence import Repository


def builtin_real_drivers() -> dict[str, AgentDriver]:
    drivers: list[AgentDriver] = [CodexDriver(), ClaudeCodeDriver(), PiDriver(), HermesDriver(), GenericCLIDriver()]
    return {driver.agent().id: driver for driver in drivers}


def builtin_test_drivers() -> dict[str, AgentDriver]:
    return {
        "fake-pass": FakeAgentDriver("fake-pass", "pass"),
        "fake-fail": FakeAgentDriver("fake-fail", "fail"),
        "fake-timeout": FakeAgentDriver("fake-timeout", "timeout"),
        "fake-crash": FakeAgentDriver("fake-crash", "crash"),
        "fake-jsonl": FakeAgentDriver("fake-jsonl", "jsonl"),
        "fake-flaky": FakeAgentDriver("fake-flaky", "flaky"),
    }


def probe_registry(repository: Repository | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for driver in builtin_real_drivers().values():
        capabilities = driver.probe()
        agent = driver.agent()
        if repository:
            repository.save_agent(agent)
        result.append({"agent": agent.to_dict(), "capabilities": capabilities.to_dict()})
    return result


def collector_status(host: str = "127.0.0.1", ports: tuple[int, ...] = (4317, 4318)) -> dict[str, Any]:
    open_ports: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                if sock.connect_ex((host, port)) == 0:
                    open_ports.append(port)
            except OSError:
                pass
    return {
        "host": host,
        "ports": list(ports),
        "open_ports": open_ports,
        "available": bool(open_ports),
        "scope": "仅限 localhost；AEL 不实现 OTLP backend。",
    }
