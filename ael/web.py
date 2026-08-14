from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .agents import builtin_real_drivers, probe_registry
from .cases import ExperimentSpec, SuiteSpec, discover_case_paths, load_case
from .comparison import compare_run_details
from .diagnosis import create_follow_up_experiment, diagnose_run
from .failures import promote_failure
from .drivers.custom import CustomCLIDriver
from .models import Agent, AgentVariant, Capabilities, ObservationProfile, RunMode
from .persistence import Repository
from .reports import matrix_report
from .runner import Runner


def create_app(root: str | Path = ".") -> FastAPI:
    repository = Repository(Path(root))
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    drivers = builtin_real_drivers()
    runner = Runner(repository, drivers)
    background_runs: dict[str, asyncio.Task[Any]] = {}
    app = FastAPI(title="Agent Eval Lab")

    def render(request: Request, name: str, **context: Any):
        status_code = context.pop("_status_code", None)
        response = templates.TemplateResponse(request=request, name=name, context=context)
        if status_code:
            response.status_code = status_code
        return response

    def agent_rows() -> list[dict[str, Any]]:
        return probe_registry(repository)

    def case_options() -> list[dict[str, Any]]:
        options: dict[tuple[str, str], dict[str, Any]] = {}
        for path in discover_case_paths(repository.root):
            try:
                case = load_case(path)
            except (OSError, ValueError):
                continue
            options[(case.id, case.revision)] = {
                "id": case.id,
                "revision": case.revision,
                "prompt": case.prompt,
                "path": str(path),
                "relative_path": str(path.relative_to(repository.root)),
                "timeout_seconds": case.timeout_seconds,
            }
        for row in repository.list_cases():
            source_value = row.get("source_path")
            if not source_value:
                continue
            source = Path(source_value).resolve()
            if not _is_within(repository.root, source) or not source.is_file():
                continue
            key = (row["id"], row["revision"])
            if key not in options:
                options[key] = {
                    "id": row["id"],
                    "revision": row["revision"],
                    "prompt": row["prompt"],
                    "path": str(source),
                    "relative_path": str(source.relative_to(repository.root)),
                    "timeout_seconds": row["timeout_seconds"],
                }
        return sorted(options.values(), key=lambda item: (item["id"], item["revision"]))

    async def run_in_background(experiment: ExperimentSpec) -> None:
        try:
            await runner.run_experiment(experiment)
        except Exception:
            repository.set_experiment_status(experiment.id, "ERROR")
            raise

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        experiments = repository.list_experiments()
        return render(
            request,
            "experiments.html",
            title="Lab",
            experiments=experiments,
            running=[item for item in experiments if item["status"] in {"PENDING", "RUNNING"}],
            recent=experiments[:8],
        )

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request):
        return render(request, "agents.html", title="Agents", agents=agent_rows())

    @app.get("/cases", response_class=HTMLResponse)
    async def cases_page(request: Request):
        return render(request, "cases.html", title="Cases", cases=case_options())

    @app.get("/experiments", response_class=HTMLResponse)
    async def experiments_page(request: Request):
        experiments = repository.list_experiments()
        return render(
            request,
            "experiments.html",
            title="Lab",
            experiments=experiments,
            running=[item for item in experiments if item["status"] in {"PENDING", "RUNNING"}],
            recent=experiments[:8],
        )

    @app.get("/experiments/new", response_class=HTMLResponse)
    async def new_experiment_page(request: Request):
        rows = agent_rows()
        return render(
            request,
            "new_experiment.html",
            title="New Experiment",
            agents=rows,
            cases=case_options(),
            selected_cases=[],
            selected_agents=[row["agent"]["id"] for row in rows if row["capabilities"]["available"]],
            error=None,
            form={},
        )

    @app.post("/experiments/new", response_class=HTMLResponse)
    async def create_experiment_page(request: Request):
        form = {
            key: values
            for key, values in parse_qs(
                (await request.body()).decode("utf-8"), keep_blank_values=True
            ).items()
        }
        rows = agent_rows()
        try:
            experiment, custom_driver = _build_experiment(repository, rows, form)
        except (TypeError, ValueError) as exc:
            return render(
                request,
                "new_experiment.html",
                title="New Experiment",
                agents=rows,
                cases=case_options(),
                selected_cases=form.get("case_path", []),
                selected_agents=form.get("agent_id", []),
                error=str(exc),
                form=form,
                _status_code=400,
            )
        repository.save_experiment(experiment, status="PENDING")
        if custom_driver:
            runner.drivers[custom_driver.agent().id] = custom_driver
            repository.save_agent(custom_driver.agent())
        background_runs[experiment.id] = asyncio.create_task(run_in_background(experiment))
        return RedirectResponse(f"/experiments/{experiment.id}", status_code=303)

    @app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
    async def experiment_page(request: Request, experiment_id: str):
        experiment = next((item for item in repository.list_experiments() if item["id"] == experiment_id), None)
        if not experiment:
            return HTMLResponse("未找到实验", status_code=404)
        runs = repository.list_runs(experiment_id)
        definition = repository.read_experiment_definition(experiment_id) or {}
        matrix = matrix_report(runs)
        variants = definition.get("variants") or []
        variant_by_id = {variant["id"]: variant for variant in variants}
        matrix["columns"] = [
            {
                "id": variant_id,
                "label": _variant_label(variant_by_id.get(variant_id, {"id": variant_id})),
                "agent": (variant_by_id.get(variant_id) or {}).get("agent_id", variant_id),
            }
            for variant_id in matrix["variant_ids"]
        ]
        return render(
            request,
            "experiment.html",
            title=f"实验 {experiment_id}",
            experiment=experiment,
            experiment_id=experiment_id,
            definition=definition,
            runs=runs,
            matrix=matrix,
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str):
        run = repository.get_run(run_id)
        if not run:
            return HTMLResponse("未找到运行记录", status_code=404)
        evidence = Path(run["run_dir"])
        changes = _read_json(evidence / "workspace" / "changes.json")
        return render(
            request,
            "run.html",
            title=f"运行 {run_id}",
            run=run,
            metadata=_read_json(evidence / "metadata.json"),
            changes=changes,
            diff=_read_text(evidence / "workspace" / "diff.patch"),
            verifier=_read_json(evidence / "verifier" / "result.json"),
            telemetry=_read_json(evidence / "telemetry" / "summary.json"),
            native_events=_read_jsonl(evidence / "native" / "events.jsonl", limit=40),
            otel_events=_read_jsonl(evidence / "telemetry" / "otel" / "events.jsonl", limit=40),
            observable_events=_observable_events(
                _read_jsonl(evidence / "telemetry" / "otel" / "events.jsonl", limit=40),
                _read_jsonl(evidence / "native" / "events.jsonl", limit=40),
            ),
            otel_raw=_read_text(evidence / "telemetry" / "otel" / "raw.jsonl", limit=30000),
            visible_changed_files=_visible_changed_files(changes),
        )

    @app.get("/runs/{run_id}/explorer", response_class=HTMLResponse)
    async def explorer_page(request: Request, run_id: str):
        if not repository.get_run(run_id):
            return HTMLResponse("未找到运行记录", status_code=404)
        explorer = compare_run_details(repository, run_id)
        reference_id = (explorer.get("matched_reference") or {}).get("run_id")
        return render(
            request,
            "explorer.html",
            title=f"失败分析器 {run_id}",
            explorer=explorer,
            diagnosis=diagnose_run(repository, run_id),
            timeline_rows=_timeline_rows(explorer),
            candidate_raw=_raw_events(repository.get_run(run_id)),
            reference_raw=_raw_events(repository.get_run(reference_id)) if reference_id else "",
        )

    @app.get("/runs/{run_id}/follow-up/new", response_class=HTMLResponse)
    async def follow_up_builder_page(request: Request, run_id: str):
        try:
            draft = create_follow_up_experiment(repository, run_id, save=False)
        except ValueError as exc:
            return HTMLResponse(str(exc), status_code=400)
        return render(
            request,
            "follow_up.html",
            title="Follow-up Experiment",
            run_id=run_id,
            draft=draft,
            agents=agent_rows(),
            error=None,
        )

    @app.post("/runs/{run_id}/follow-up")
    async def follow_up_page(request: Request, run_id: str):
        form = {
            key: values
            for key, values in parse_qs(
                (await request.body()).decode("utf-8"), keep_blank_values=True
            ).items()
        }
        independent_variable = (form.get("independent_variable") or ["verification_gate"])[-1]
        candidate_agent_id = (form.get("candidate_agent_id") or [None])[-1] or None
        try:
            if independent_variable == "agent" and candidate_agent_id:
                available = {
                    row["agent"]["id"]
                    for row in agent_rows()
                    if row["capabilities"].get("available")
                }
                if candidate_agent_id not in available:
                    raise ValueError(f"Candidate Agent 不可用：{candidate_agent_id}")
            experiment = create_follow_up_experiment(
                repository,
                run_id,
                independent_variable=independent_variable,
                candidate_agent_id=candidate_agent_id,
                trials=int((form.get("trials") or ["2"])[-1]),
                max_concurrency=int((form.get("max_concurrency") or ["2"])[-1]),
                save=False,
            )
        except (TypeError, ValueError) as exc:
            try:
                draft = create_follow_up_experiment(repository, run_id, save=False)
            except ValueError:
                return HTMLResponse(str(exc), status_code=400)
            return render(
                request,
                "follow_up.html",
                title="Follow-up Experiment",
                run_id=run_id,
                draft=draft,
                agents=agent_rows(),
                error=str(exc),
                _status_code=400,
            )
        repository.save_experiment(experiment, status="PENDING", follow_up_of=run_id)
        background_runs[experiment.id] = asyncio.create_task(run_in_background(experiment))
        return RedirectResponse(f"/experiments/{experiment.id}", status_code=303)

    @app.get("/failures", response_class=HTMLResponse)
    async def failures_page(request: Request):
        return render(request, "failures.html", title="Failure Patterns", failures=repository.list_failures())

    @app.get("/failures/{failure_id}", response_class=HTMLResponse)
    async def failure_page(request: Request, failure_id: str):
        failure = repository.get_failure(failure_id)
        if not failure:
            return HTMLResponse("未找到失败记录", status_code=404)
        source_run = repository.get_run(failure["source_run_id"])
        explorer = compare_run_details(repository, source_run["id"]) if source_run else {}
        return render(request, "failure.html", title=f"失败模式 {failure_id}", failure=failure, explorer=explorer)

    @app.post("/failures/{failure_id}/promote")
    async def promote_failure_page(request: Request, failure_id: str):
        try:
            promote_failure(repository, failure_id)
        except (OSError, ValueError) as exc:
            return HTMLResponse(str(exc), status_code=400)
        return RedirectResponse(f"/failures/{failure_id}", status_code=303)

    return app


def _build_experiment(
    repository: Repository,
    agent_rows: list[dict[str, Any]],
    form: dict[str, list[str]],
) -> tuple[ExperimentSpec, CustomCLIDriver | None]:
    def first(name: str, default: str = "") -> str:
        return (form.get(name) or [default])[-1].strip()

    allowed_paths = set(discover_case_paths(repository.root))
    for row in repository.list_cases():
        source_value = row.get("source_path")
        if not source_value:
            continue
        source = Path(source_value).resolve()
        if _is_within(repository.root, source) and source.is_file():
            allowed_paths.add(source)

    case_paths = [Path(value).resolve() for value in form.get("case_path", []) if value]
    if not case_paths:
        raise ValueError("至少选择一个 Case")
    unregistered = [path for path in case_paths if path not in allowed_paths]
    if unregistered:
        raise ValueError(f"Case 必须来自当前工作区已注册的 Case：{unregistered[0]}")
    cases = []
    for path in case_paths:
        try:
            cases.append(load_case(path))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Case 不可用：{path}（{exc}）") from exc

    selected_agents = form.get("agent_id", [])
    if not selected_agents:
        raise ValueError("至少选择一个可用 Agent")
    capabilities = {row["agent"]["id"]: row["capabilities"] for row in agent_rows}
    variants = []
    custom_driver = None
    for agent_id in selected_agents:
        if agent_id == "custom-harness":
            command_text = first("custom_command")
            if not command_text:
                raise ValueError("选择 Custom Harness 后必须提供真实可执行命令")
            try:
                command = shlex.split(command_text)
            except ValueError as exc:
                raise ValueError(f"Custom Harness command 不合法：{exc}") from exc
            if not command or not (Path(command[0]).exists() or shutil.which(command[0])):
                raise ValueError(f"Custom Harness command 不可执行：{command[0] if command else command_text}")
            custom_agent = Agent(
                id="custom-harness",
                display_name="Custom Harness",
                driver="custom",
                binary=command[0],
                detected_version="configured",
                capabilities=Capabilities(
                    available=True,
                    version="configured",
                    supports_models=False,
                    supports_controlled=False,
                    controlled_support="UNKNOWN",
                    supports_telemetry=False,
                    supports_deep=False,
                    notes=("由用户在此实验中提供的本地命令；AEL 只记录可观察 native output。",),
                ).to_dict(),
            )
            custom_driver = CustomCLIDriver(custom_agent, command)
            variants.append(
                AgentVariant(
                    id="custom-harness-default",
                    agent_id="custom-harness",
                    model="default",
                    provider="default",
                    run_mode=RunMode.NATIVE,
                    observation_profile=ObservationProfile.MINIMAL,
                )
            )
            continue
        capability = capabilities.get(agent_id)
        if not capability or not capability.get("available"):
            raise ValueError(f"Agent 不可用：{agent_id}")
        model = first(f"model_{agent_id}", "default") or "default"
        provider = first(f"provider_{agent_id}", "default") or "default"
        if model.lower() not in {"default", "unknown"} and not capability.get("supports_models"):
            raise ValueError(f"{agent_id} 不支持 model switching；不能创建该组合")
        try:
            run_mode = RunMode(first(f"run_mode_{agent_id}", "native"))
            observation_profile = ObservationProfile(first(f"observation_profile_{agent_id}", "minimal"))
        except ValueError as exc:
            raise ValueError(f"{agent_id} 的 run mode 或 observation profile 不合法") from exc
        config_text = first(f"config_{agent_id}", "")
        try:
            harness_config = json.loads(config_text) if config_text else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"{agent_id} 的 Agent config 必须是 JSON object") from exc
        if not isinstance(harness_config, dict):
            raise ValueError(f"{agent_id} 的 Agent config 必须是 JSON object")
        model_slug = _slug(model if model.lower() not in {"default", "unknown"} else "default")
        variants.append(
            AgentVariant(
                id=f"{agent_id}-{model_slug}",
                agent_id=agent_id,
                model=model,
                provider=provider,
                model_config={},
                harness_config=harness_config,
                run_mode=run_mode,
                observation_profile=observation_profile,
            )
        )
    name = _slug(first("experiment_name", "golden-coding"))
    suite_id = _slug(first("suite_id", "golden-coding"))
    experiment_id = f"{name}-{uuid.uuid4().hex[:8]}"
    return (
        ExperimentSpec(
            id=experiment_id,
            suite=SuiteSpec(suite_id, "coding", tuple(cases)),
            variants=tuple(variants),
            trials=max(1, int(first("trials", "1"))),
            max_concurrency=max(1, int(first("max_concurrency", "1"))),
            metadata={
                "created_from": "web_builder",
                "selected_case_paths": [str(path) for path in case_paths],
                "agent_ids": selected_agents,
                "custom_harness_configured": bool(custom_driver),
            },
        ),
        custom_driver,
    )


def _variant_label(variant: dict[str, Any]) -> str:
    agent = variant.get("agent_id") or variant.get("id") or "Variant"
    model = variant.get("model") or "default"
    label = f"{agent} / {('Default configured' if str(model).lower() in {'default', 'unknown'} else model)}"
    config = variant.get("harness_config") or {}
    if config.get("verification_gate") is True:
        label += " · verification_gate=on"
    elif config.get("verification_gate") is False:
        label += " · verification_gate=off"
    return label


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _timeline_rows(explorer: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = (explorer.get("timeline_diff") or {}).get("candidate") or []
    reference = (explorer.get("timeline_diff") or {}).get("reference") or []
    return [
        {
            "candidate": candidate[index] if index < len(candidate) else None,
            "reference": reference[index] if index < len(reference) else None,
        }
        for index in range(max(len(candidate), len(reference)))
    ]


def _raw_events(run: dict[str, Any] | None) -> str:
    if not run:
        return ""
    path = Path(run["run_dir"]) / "native" / "raw.jsonl"
    return _read_text(path, limit=30000)


def _visible_changed_files(changes: dict[str, Any]) -> list[str]:
    return [
        path
        for path in (changes.get("changed_files") or [])
        if not path.startswith("__pycache__/")
        and not path.startswith(".pytest_cache/")
        and not path.endswith((".pyc", ".pyo"))
    ]


def _observable_events(
    otel_events: list[dict[str, Any]],
    native_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events = otel_events or native_events
    actionable = {"tool_call", "tool_result", "command", "file_change", "verification"}
    filtered = [event for event in events if event.get("kind") in actionable]
    try:
        filtered.sort(key=lambda event: int(str(event.get("timestamp") or "0")))
    except ValueError:
        pass
    return filtered or events[:20]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower() or "experiment"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {"value": value}


def _read_text(path: Path, limit: int = 20000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def _read_jsonl(path: Path, limit: int = 40) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:limit]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result
