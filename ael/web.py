from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .agents import probe_registry
from .comparison import compare_run_details
from .diagnosis import create_follow_up_experiment, diagnose_run
from .failures import promote_failure
from .persistence import Repository
from .reports import matrix_report


def create_app(root: str | Path = ".") -> FastAPI:
    repository = Repository(Path(root))
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app = FastAPI(title="Agent Eval Lab")

    def render(request: Request, name: str, **context: Any):
        return templates.TemplateResponse(request=request, name=name, context=context)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return RedirectResponse("/experiments", status_code=303)

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request):
        rows = probe_registry(repository)
        return render(request, "agents.html", title="Agents", agents=rows)

    @app.get("/cases", response_class=HTMLResponse)
    async def cases_page(request: Request):
        return render(request, "cases.html", title="Cases", cases=repository.list_cases())

    @app.get("/experiments", response_class=HTMLResponse)
    async def experiments_page(request: Request):
        return render(request, "experiments.html", title="Experiments", experiments=repository.list_experiments())

    @app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
    async def experiment_page(request: Request, experiment_id: str):
        runs = repository.list_runs(experiment_id)
        return render(
            request,
            "experiment.html",
            title=f"Experiment {experiment_id}",
            experiment_id=experiment_id,
            runs=runs,
            matrix=matrix_report(runs),
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str):
        run = repository.get_run(run_id)
        if not run:
            return HTMLResponse("Run not found", status_code=404)
        evidence = Path(run["run_dir"])
        return render(
            request,
            "run.html",
            title=f"Run {run_id}",
            run=run,
            metadata=_read_json(evidence / "metadata.json"),
            native=_read_text(evidence / "native" / "events.jsonl"),
            diff=_read_text(evidence / "workspace" / "diff.patch"),
            verifier=_read_json(evidence / "verifier" / "result.json"),
        )

    @app.get("/runs/{run_id}/explorer", response_class=HTMLResponse)
    async def explorer_page(request: Request, run_id: str):
        if not repository.get_run(run_id):
            return HTMLResponse("Run not found", status_code=404)
        return render(
            request,
            "explorer.html",
            title=f"Failure Explorer {run_id}",
            explorer=compare_run_details(repository, run_id),
            diagnosis=diagnose_run(repository, run_id),
        )

    @app.post("/runs/{run_id}/follow-up")
    async def follow_up_page(request: Request, run_id: str):
        try:
            experiment = create_follow_up_experiment(repository, run_id)
        except ValueError as exc:
            return HTMLResponse(str(exc), status_code=400)
        return RedirectResponse(f"/experiments/{experiment.id}", status_code=303)

    @app.get("/failures", response_class=HTMLResponse)
    async def failures_page(request: Request):
        return render(request, "failures.html", title="Failures", failures=repository.list_failures())

    @app.get("/failures/{failure_id}", response_class=HTMLResponse)
    async def failure_page(request: Request, failure_id: str):
        failure = repository.get_failure(failure_id)
        if not failure:
            return HTMLResponse("Failure not found", status_code=404)
        source_run = repository.get_run(failure["source_run_id"])
        explorer = compare_run_details(repository, source_run["id"]) if source_run else {}
        return render(request, "failure.html", title=f"Failure {failure_id}", failure=failure, explorer=explorer)

    @app.post("/failures/{failure_id}/promote")
    async def promote_failure_page(request: Request, failure_id: str):
        try:
            promote_failure(repository, failure_id)
        except (OSError, ValueError) as exc:
            return HTMLResponse(str(exc), status_code=400)
        return RedirectResponse(f"/failures/{failure_id}", status_code=303)

    return app


def _read_text(path: Path, limit: int = 20000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {"value": value}
