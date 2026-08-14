from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from .agents import builtin_real_drivers, builtin_test_drivers, collector_status, probe_registry
from .cases import load_experiment
from .persistence import Repository
from .reports import matrix_report
from .runner import Runner

app = typer.Typer(add_completion=False, help="Agent Eval Lab local experiment CLI.")


def _drivers() -> dict[str, Any]:
    result = {}
    result.update(builtin_real_drivers())
    result.update(builtin_test_drivers())
    return result


@app.command()
def doctor(root: Path = typer.Option(Path("."), "--root", help="AEL project root.")) -> None:
    repository = Repository(root)
    rows = probe_registry(repository)
    print("AEL doctor")
    print(f"database: OK ({repository.db_path})")
    for row in rows:
        agent = row["agent"]
        capabilities = row["capabilities"]
        state = "AVAILABLE" if capabilities["available"] else "UNAVAILABLE"
        print(
            f"{agent['id']}: {state} version={agent['detected_version']} "
            f"controlled={capabilities['controlled_support']}"
        )
    collector = collector_status()
    collector_state = "AVAILABLE" if collector["available"] else "NOT_FOUND"
    print(f"otel collector: {collector_state} host={collector['host']} ports={collector['ports']}")


@app.command()
def agents(root: Path = typer.Option(Path("."), "--root", help="AEL project root.")) -> None:
    repository = Repository(root)
    rows = probe_registry(repository)
    print(json.dumps(rows, indent=2, sort_keys=True))


@app.command("run")
def run_experiment(
    experiment: Path = typer.Argument(..., exists=True, readable=True),
    root: Path = typer.Option(Path("."), "--root", help="AEL project root."),
) -> None:
    spec = load_experiment(experiment)
    runner = Runner(Repository(root), _drivers())
    results = asyncio.run(runner.run_experiment(spec))
    print(json.dumps({"experiment": spec.id, "matrix": matrix_report(results), "runs": results}, indent=2, sort_keys=True))


@app.command("compare")
def compare(
    experiment_a: str = typer.Argument(...),
    experiment_b: str = typer.Argument(...),
    root: Path = typer.Option(Path("."), "--root", help="AEL project root."),
) -> None:
    from .comparison import compare_experiments

    report = compare_experiments(Repository(root), experiment_a, experiment_b)
    print(json.dumps(report, indent=2, sort_keys=True))


@app.command()
def ui(
    root: Path = typer.Option(Path("."), "--root", help="AEL project root."),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8711, "--port"),
) -> None:
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(root), host=host, port=port)


if __name__ == "__main__":
    app()

