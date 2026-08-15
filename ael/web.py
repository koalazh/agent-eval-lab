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
from .comparison import build_experiment_comparison, compare_run_details
from .diagnosis import create_follow_up_experiment, diagnose_run
from .failures import promote_failure
from .drivers.custom import CustomCLIDriver
from .models import Agent, AgentVariant, Capabilities, ObservationProfile, RunMode
from .persistence import Repository
from .reports import matrix_report
from .runner import Runner
from .trace_view import (
    align_trajectories,
    build_evidence_sources,
    build_file_activity,
    build_telemetry_overview,
    build_trace_view,
    build_trajectory,
    build_verifier_phases,
    build_otel_status,
)


def _case_options(repository: Repository) -> list[dict[str, Any]]:
    """Return one current runnable option plus historical, read-only revisions."""
    options: dict[tuple[str, str], dict[str, Any]] = {}
    for path in discover_case_paths(repository.root):
        try:
            case = load_case(path)
        except (OSError, ValueError):
            continue
        if case.id == "verify-answer-001":
            continue
        options[(case.id, case.revision)] = {
            "id": case.id,
            "revision": case.revision,
            "prompt": case.prompt,
            "path": str(path),
            "relative_path": str(path.relative_to(repository.root)),
            "timeout_seconds": case.timeout_seconds,
            "runnable": True,
            "is_current": True,
            "revision_note": "当前可执行版本",
        }
    for row in repository.list_cases():
        source_value = row.get("source_path")
        if not source_value or row["id"] == "verify-answer-001":
            continue
        source = Path(source_value).resolve()
        if not _is_within(repository.root, source) or not source.is_file():
            continue
        key = (row["id"], row["revision"])
        if key in options:
            continue
        options[key] = {
            "id": row["id"],
            "revision": row["revision"],
            "prompt": row["prompt"],
            "path": str(source),
            "relative_path": str(source.relative_to(repository.root)),
            "timeout_seconds": row["timeout_seconds"],
            "runnable": False,
            "is_current": False,
            "revision_note": "历史 Run 版本（当前工作区没有可执行快照）",
        }
    return sorted(options.values(), key=lambda item: (item["id"], not item["is_current"], item["revision"]))


def _case_catalog(repository: Repository, *, include_archived: bool = False) -> list[dict[str, Any]]:
    options = _case_options(repository)
    catalog = {row["id"]: row for row in repository.list_case_catalog()}
    grouped: dict[str, dict[str, Any]] = {}
    for option in options:
        group = grouped.setdefault(
            option["id"],
            {
                "id": option["id"],
                "display_name": catalog.get(option["id"], {}).get("display_name") or option["id"],
                "notes": catalog.get(option["id"], {}).get("notes") or "",
                "status": catalog.get(option["id"], {}).get("status") or "ACTIVE",
                "source_path": option["relative_path"],
                "revisions": [],
            },
        )
        group["revisions"].append(option)
        if option["is_current"]:
            group["current"] = option
    result = [group for group in grouped.values() if include_archived or group["status"] != "ARCHIVED"]
    for group in result:
        group.setdefault("current", group["revisions"][0] if group["revisions"] else {})
        group["revision_count"] = len(group["revisions"])
        group["runnable"] = bool(group["current"].get("runnable"))
    return sorted(result, key=lambda item: item["id"])


def _case_groups(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for case in cases:
        group = groups.setdefault(
            case["id"],
            {"id": case["id"], "prompt": case["prompt"], "revisions": [], "selected_revision": case["revision"]},
        )
        group["revisions"].append(case)
        if case.get("is_current"):
            group["selected_revision"] = case["revision"]
            group["prompt"] = case["prompt"]
    return sorted(groups.values(), key=lambda item: item["id"])


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
        return [
            option
            for group in _case_catalog(repository)
            for option in group["revisions"]
        ]

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
            title="实验室",
            experiments=experiments,
            experiment_cards=_experiment_cards(repository, experiments),
            running=[item for item in experiments if item["status"] in {"PENDING", "RUNNING"}],
            recent=experiments[:8],
        )

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request):
        return render(request, "agents.html", title="Agent 配置", agents=agent_rows())

    @app.get("/cases", response_class=HTMLResponse)
    async def cases_page(request: Request):
        return render(
            request,
            "cases.html",
            title="Case 管理",
            cases=_case_catalog(repository, include_archived=True),
            active_case_count=sum(group["status"] != "ARCHIVED" for group in _case_catalog(repository, include_archived=True)),
            archived_case_count=sum(group["status"] == "ARCHIVED" for group in _case_catalog(repository, include_archived=True)),
        )

    @app.get("/cases/new", response_class=HTMLResponse)
    async def new_case_page(request: Request):
        return render(request, "case_form.html", title="登记 Case", form={}, error=None)

    @app.post("/cases/new", response_class=HTMLResponse)
    async def create_case_page(request: Request):
        form = {
            key: values
            for key, values in parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True).items()
        }
        try:
            case_path = _resolve_case_path(repository.root, (form.get("case_path") or [""])[-1])
            case = load_case(case_path)
            existing = repository.get_case_catalog(case.id)
            if existing and existing.get("status") != "ARCHIVED":
                raise ValueError(f"Case 已在目录中：{case.id}")
            repository.save_case_catalog(
                case.id,
                source_path=str(case_path),
                display_name=(form.get("display_name") or [case.id])[-1].strip() or case.id,
                notes=(form.get("notes") or [""])[-1].strip(),
                status="ACTIVE",
            )
        except (OSError, ValueError) as exc:
            return render(request, "case_form.html", title="登记 Case", form=form, error=str(exc), _status_code=400)
        return RedirectResponse(f"/cases/{case.id}", status_code=303)

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    async def case_detail_page(request: Request, case_id: str):
        group = next((item for item in _case_catalog(repository, include_archived=True) if item["id"] == case_id), None)
        if not group:
            return HTMLResponse("未找到 Case", status_code=404)
        revision = request.query_params.get("revision")
        selected = next((item for item in group["revisions"] if item["revision"] == revision), None) if revision else group["current"]
        selected = selected or group["current"]
        return render(request, "case_detail.html", title=f"Case {case_id}", case=group, selected_revision=selected)

    @app.post("/cases/{case_id}")
    async def update_case_page(request: Request, case_id: str):
        form = {
            key: values
            for key, values in parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True).items()
        }
        action = (form.get("action") or ["update"])[-1]
        group = next((item for item in _case_catalog(repository, include_archived=True) if item["id"] == case_id), None)
        if not group:
            return HTMLResponse("未找到 Case", status_code=404)
        if action == "archive":
            status = "ARCHIVED"
        elif action == "restore":
            status = "ACTIVE"
        else:
            status = group["status"]
        repository.save_case_catalog(
            case_id,
            source_path=group["current"].get("path"),
            display_name=(form.get("display_name") or [group["display_name"]])[-1].strip() or case_id,
            notes=(form.get("notes") or [group["notes"]])[-1].strip(),
            status=status,
        )
        return RedirectResponse(f"/cases/{case_id}", status_code=303)

    @app.get("/experiments", response_class=HTMLResponse)
    async def experiments_page(request: Request):
        experiments = repository.list_experiments()
        return render(
            request,
            "experiments.html",
            title="实验室",
            experiments=experiments,
            experiment_cards=_experiment_cards(repository, experiments),
            running=[item for item in experiments if item["status"] in {"PENDING", "RUNNING"}],
            recent=experiments[:8],
        )

    @app.get("/experiments/new", response_class=HTMLResponse)
    async def new_experiment_page(request: Request):
        rows = agent_rows()
        cases = case_options()
        case_groups = _case_groups(cases)
        requested_case = request.query_params.get("case_id")
        selected_case_ids = [
            group["id"] for group in case_groups if requested_case and group["id"] == requested_case
        ]
        return render(
            request,
            "new_experiment.html",
            title="新建实验",
            agents=rows,
            cases=cases,
            case_groups=case_groups,
            selected_cases=selected_case_ids,
            selected_revisions={group["id"]: group["selected_revision"] for group in case_groups},
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
                title="新建实验",
                agents=rows,
                cases=case_options(),
                case_groups=_case_groups(case_options()),
                selected_cases=_selected_case_ids(form),
                selected_revisions=_selected_revisions(form),
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
        comparison = build_experiment_comparison(repository, runs, variants)
        return render(
            request,
            "experiment.html",
            title=f"实验 {experiment_id}",
            experiment=experiment,
            experiment_id=experiment_id,
            experiment_status_label=_status_label(experiment.get("status")),
            definition=definition,
            runs=runs,
            matrix=matrix,
            comparison=comparison,
            differential_rows=[row for row in matrix["matrix_rows"] if row["differential"]],
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str):
        run = repository.get_run(run_id)
        if not run:
            return HTMLResponse("未找到运行记录", status_code=404)
        evidence = Path(run["run_dir"])
        changes = _read_json(evidence / "workspace" / "changes.json")
        verifier = _read_json(evidence / "verifier" / "result.json")
        telemetry = _read_json(evidence / "telemetry" / "summary.json")
        metadata = _read_json(evidence / "metadata.json")
        native_events = _read_jsonl(evidence / "native" / "events.jsonl", limit=None)
        otel_events = _read_jsonl(evidence / "telemetry" / "otel" / "events.jsonl", limit=None)
        trace_view = build_trace_view(otel_events, native_events)
        visible_changed_files = _visible_changed_files(changes)
        return render(
            request,
            "run.html",
            title=f"运行 {run_id}",
            run=run,
            run_status_label=_status_label(run.get("run_status")),
            metadata=metadata,
            run_duration_label=_duration_label(metadata.get("duration_seconds")),
            changes=changes,
            diff=_read_text(evidence / "workspace" / "diff.patch"),
            verifier=verifier,
            verifier_phases=build_verifier_phases(verifier),
            telemetry=telemetry,
            native_events=native_events,
            otel_events=otel_events,
            trace_view=trace_view,
            otel_status=build_otel_status(run, telemetry, otel_events, trace_view),
            telemetry_overview=build_telemetry_overview(telemetry, otel_events, trace_view, native_events),
            evidence_sources=build_evidence_sources(
                run,
                verifier,
                visible_changed_files,
                telemetry,
                trace_view,
                workspace_observed=(evidence / "workspace" / "changes.json").exists(),
            ),
            trajectory_steps=build_trajectory(
                otel_events,
                native_events,
                verifier=verifier,
                changed_files=visible_changed_files,
            ),
            file_activity=build_file_activity(
                native_events,
                otel_events,
                visible_changed_files,
                run_id=run_id,
            ),
            otel_raw=_read_text(evidence / "telemetry" / "otel" / "raw.jsonl", limit=30000),
            native_raw=_read_text(evidence / "native" / "raw.jsonl", limit=30000),
            visible_changed_files=visible_changed_files,
        )

    @app.get("/runs/{run_id}/explorer", response_class=HTMLResponse)
    async def explorer_page(request: Request, run_id: str):
        if not repository.get_run(run_id):
            return HTMLResponse("未找到运行记录", status_code=404)
        explorer = compare_run_details(repository, run_id)
        reference_id = (explorer.get("matched_reference") or {}).get("run_id")
        candidate_run = repository.get_run(run_id)
        reference_run = repository.get_run(reference_id) if reference_id else None
        for summary in (explorer.get("candidate_summary"), explorer.get("reference_summary")):
            if summary:
                summary["duration_label"] = _duration_label(summary.get("duration_seconds"))
        candidate_trace = _trace_for_run(candidate_run)
        reference_trace = _trace_for_run(reference_run)
        candidate_trajectory = _trajectory_for_run(candidate_run)
        reference_trajectory = _trajectory_for_run(reference_run)
        return render(
            request,
            "explorer.html",
            title=f"失败分析器 {run_id}",
            explorer=explorer,
            diagnosis=diagnose_run(repository, run_id),
            timeline_rows=_timeline_rows(explorer),
            candidate_trace=candidate_trace,
            reference_trace=reference_trace,
            trajectory_rows=align_trajectories(candidate_trajectory, reference_trajectory),
            candidate_evidence_sources=_evidence_for_run(candidate_run),
            reference_evidence_sources=_evidence_for_run(reference_run),
            candidate_raw=_raw_events(candidate_run),
            reference_raw=_raw_events(reference_run),
            candidate_otel_raw=_raw_otel_events(candidate_run),
            reference_otel_raw=_raw_otel_events(reference_run),
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
            title="后续实验",
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
                title="后续实验",
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
        failures = _failure_rollups(repository.list_failures(), repository)
        return render(request, "failures.html", title="失败模式", failures=failures, failure_summary=_failure_summary(failures))

    @app.get("/failures/{failure_id}", response_class=HTMLResponse)
    async def failure_page(request: Request, failure_id: str):
        failure = next(
            (
                item
                for item in _failure_rollups(repository.list_failures(), repository)
                if failure_id in item.get("rollup_failure_ids", [])
            ),
            None,
        )
        if failure is None:
            failure = repository.get_failure(failure_id)
        if not failure:
            return HTMLResponse("未找到失败记录", status_code=404)
        source_run = repository.get_run(failure["source_run_id"])
        verifier = (source_run or {}).get("verifier") or {}
        signature_source = failure["details"].get("verifier_signature_text") or " ".join(
            str(verifier.get(key) or "") for key in ("stdout", "stderr")
        )
        failure["details"]["display_summary"] = _failure_display_summary(signature_source)
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

    case_paths: list[Path] = []
    revision_fields = {
        key.removeprefix("case_revision__"): values[-1]
        for key, values in form.items()
        if key.startswith("case_revision__") and values and values[-1]
    }
    selected_case_ids = {
        key.removeprefix("case_selected__")
        for key, values in form.items()
        if key.startswith("case_selected__") and values and values[-1]
    }
    if selected_case_ids:
        revision_fields = {
            case_id: revision
            for case_id, revision in revision_fields.items()
            if case_id in selected_case_ids
        }
    if revision_fields:
        available = {(option["id"], option["revision"]): option for option in _case_options(repository)}
        catalog = {row["id"]: row for row in repository.list_case_catalog()}
        for case_id, revision in revision_fields.items():
            if catalog.get(case_id, {}).get("status") == "ARCHIVED":
                raise ValueError(f"Case 已归档：{case_id}；请先恢复后再运行实验")
            option = available.get((case_id, revision))
            if not option:
                raise ValueError(f"Case revision 不存在：{case_id} / {revision[:12]}")
            if not option.get("runnable"):
                raise ValueError(f"Case revision {revision[:12]} 只有历史证据，当前工作区没有可执行快照；请选择最新版本")
            case_paths.append(Path(option["path"]).resolve())
    else:
        case_paths = [_resolve_case_path(repository.root, value) for value in form.get("case_path", []) if value]
    if not case_paths:
        raise ValueError("至少选择一个 Case")
    cases = []
    selected_case_ids: set[str] = set()
    for path in case_paths:
        try:
            case = load_case(path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Case 不可用：{path}（{exc}）") from exc
        if case.id == "verify-answer-001":
            raise ValueError("旧 smoke Case 不属于真实编码任务 Case 实验路径")
        if case.id in selected_case_ids:
            raise ValueError(f"同一个 Case 只能选择一个 revision：{case.id}")
        selected_case_ids.add(case.id)
        cases.append(case)

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
                raise ValueError("选择自定义 Harness 后必须提供真实可执行命令")
            try:
                command = shlex.split(command_text)
            except ValueError as exc:
                raise ValueError(f"自定义 Harness 命令不合法：{exc}") from exc
            if not command or not (Path(command[0]).exists() or shutil.which(command[0])):
                raise ValueError(f"自定义 Harness 命令不可执行：{command[0] if command else command_text}")
            custom_agent = Agent(
                id="custom-harness",
                display_name="自定义 Harness",
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
            raise ValueError(f"{agent_id} 不支持模型切换；不能创建该组合")
        try:
            run_mode = RunMode(first(f"run_mode_{agent_id}", "native"))
            observation_profile = ObservationProfile(first(f"observation_profile_{agent_id}", "minimal"))
        except ValueError as exc:
            raise ValueError(f"{agent_id} 的运行模式或观测配置不合法") from exc
        config_text = first(f"config_{agent_id}", "")
        try:
            harness_config = json.loads(config_text) if config_text else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"{agent_id} 的 Agent 配置必须是 JSON 对象") from exc
        if not isinstance(harness_config, dict):
            raise ValueError(f"{agent_id} 的 Agent 配置必须是 JSON 对象")
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
    label = f"{agent} / {('默认配置' if str(model).lower() in {'default', 'unknown'} else model)}"
    config = variant.get("harness_config") or {}
    if config.get("verification_gate") is True:
        label += " · verification_gate=on"
    elif config.get("verification_gate") is False:
        label += " · verification_gate=off"
    return label


def _experiment_cards(repository: Repository, experiments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for experiment in experiments:
        runs = repository.list_runs(experiment["id"])
        matrix = matrix_report(runs)
        differential = [row["case_id"] for row in matrix["matrix_rows"] if row["differential"]]
        cards.append(
            {
                **experiment,
                "created_at_label": str(experiment.get("created_at") or "").replace("T", " ")[:16],
                "status_label": _status_label(experiment.get("status")),
                "run_count": len(runs),
                "pass_rate": matrix["summary"]["pass_rate"],
                "differential_cases": differential,
                "completed_runs": sum(run.get("run_status") == "COMPLETED" for run in runs),
            }
        )
    return cards


def _selected_revisions(form: dict[str, list[str]]) -> dict[str, str]:
    return {
        key.removeprefix("case_revision__"): values[-1]
        for key, values in form.items()
        if key.startswith("case_revision__") and values and values[-1]
    }


def _selected_case_ids(form: dict[str, list[str]]) -> list[str]:
    selected = [
        key.removeprefix("case_selected__")
        for key, values in form.items()
        if key.startswith("case_selected__") and values and values[-1]
    ]
    if selected:
        return selected
    return [
        case_id
        for case_id, _ in _selected_revisions(form).items()
    ]


def _resolve_case_path(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not _is_within(root, candidate) or not candidate.is_file() or candidate.name != "case.yaml":
        raise ValueError(f"Case 必须来自当前工作区已注册且可读的 case.yaml：{value}")
    if candidate not in set(discover_case_paths(root)):
        raise ValueError(f"Case 必须来自当前工作区已注册且已发现的 Case：{value}")
    return candidate


def _failure_summary(failures: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"total": len(failures), "observed": 0, "reproduced": 0, "fixed": 0, "guarded": 0}
    for failure in failures:
        status = str(failure.get("status") or "").upper()
        if status == "OBSERVED":
            counts["observed"] += 1
        elif status == "REPRODUCED":
            counts["reproduced"] += 1
        elif status == "FIXED":
            counts["fixed"] += 1
        elif status == "REGRESSION_GUARDED":
            counts["guarded"] += 1
    return counts


_FAILURE_STATUS_RANK = {
    "OBSERVED": 1,
    "REPRODUCED": 2,
    "FIXED": 3,
    "REGRESSION_GUARDED": 4,
}


def _failure_rollups(failures: list[dict[str, Any]], repository: Repository | None = None) -> list[dict[str, Any]]:
    """Present old per-run rows as case-revision patterns without mutating evidence."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for failure in failures:
        details = failure.get("details") or {}
        key = (
            str(details.get("case_id") or "unknown"),
            str(details.get("case_revision") or "unknown"),
            str(details.get("verifier_signature_text") or "unknown"),
        )
        groups.setdefault(key, []).append(failure)

    rollups: list[dict[str, Any]] = []
    for (case_id, case_revision, verifier_signature), items in groups.items():
        representative = max(
            items,
            key=lambda item: (
                _FAILURE_STATUS_RANK.get(str(item.get("status") or "").upper(), 0),
                str(item.get("updated_at") or item.get("created_at") or ""),
            ),
        )
        aggregate = dict(representative)
        details = dict(representative.get("details") or {})
        run_ids: list[str] = []
        variant_ids: list[str] = []
        experiment_ids: list[str] = []
        for item in items:
            item_details = item.get("details") or {}
            for run_id in item.get("run_ids") or item_details.get("run_ids") or [item.get("source_run_id")]:
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)
            for variant_id in item_details.get("variant_ids") or [item_details.get("variant_id")]:
                if variant_id and variant_id not in variant_ids:
                    variant_ids.append(variant_id)
            for experiment_id in item_details.get("experiment_ids") or [item_details.get("experiment_id")]:
                if experiment_id and experiment_id not in experiment_ids:
                    experiment_ids.append(experiment_id)
        status = max(
            (str(item.get("status") or "OBSERVED").upper() for item in items),
            key=lambda value: _FAILURE_STATUS_RANK.get(value, 0),
        )
        if status == "OBSERVED" and len(run_ids) >= 2:
            status = "REPRODUCED"
        details.update(
            {
                "case_id": case_id,
                "case_revision": case_revision,
                "verifier_signature_text": "" if verifier_signature == "unknown" else verifier_signature,
                "run_ids": run_ids,
                "run_count": len(run_ids),
                "variant_ids": sorted(variant_ids),
                "experiment_ids": sorted(experiment_ids),
                "rollup_count": len(items),
                "rollup_scope": "同一 Case revision / verifier signature",
            }
        )
        signature_source = details.get("verifier_signature_text") or ""
        if not signature_source and repository:
            source_run = repository.get_run(str(representative.get("source_run_id") or ""))
            verifier = (source_run or {}).get("verifier") or {}
            signature_source = " ".join(str(verifier.get(key) or "") for key in ("stdout", "stderr"))
        details["display_summary"] = _failure_display_summary(signature_source)
        aggregate["status"] = status
        aggregate["run_ids"] = run_ids
        aggregate["details"] = details
        aggregate["rollup_failure_ids"] = [str(item.get("id")) for item in items if item.get("id")]
        rollups.append(aggregate)
    return sorted(
        rollups,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )


def _failure_display_summary(value: Any) -> str:
    """Turn verifier output into a compact product-facing failure signal."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return "未捕获 verifier 失败摘要"
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*Error|AssertionError):\s*(.*?)(?=\s+File \"|$)", text)
    if match:
        return f"{match.group(1)}：{match.group(2)[:220]}"
    failed_test = re.search(r"FAILED\s+([^\s]+)::([^\s]+)", text)
    if failed_test:
        return f"测试失败：{failed_test.group(1)}::{failed_test.group(2)}"
    text = text.split(" Traceback", 1)[0].strip()
    return text[:220]


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


def _raw_otel_events(run: dict[str, Any] | None) -> str:
    if not run:
        return ""
    path = Path(run["run_dir"]) / "telemetry" / "otel" / "raw.jsonl"
    return _read_text(path, limit=30000)


def _trace_for_run(run: dict[str, Any] | None) -> dict[str, Any]:
    if not run:
        return build_trace_view([], [])
    evidence = Path(run["run_dir"])
    return build_trace_view(
        _read_jsonl(evidence / "telemetry" / "otel" / "events.jsonl", limit=None),
        _read_jsonl(evidence / "native" / "events.jsonl", limit=None),
    )


def _evidence_for_run(run: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not run:
        return []
    evidence = Path(run["run_dir"])
    changes = _read_json(evidence / "workspace" / "changes.json")
    verifier = _read_json(evidence / "verifier" / "result.json")
    telemetry = _read_json(evidence / "telemetry" / "summary.json")
    otel_events = _read_jsonl(evidence / "telemetry" / "otel" / "events.jsonl", limit=None)
    native_events = _read_jsonl(evidence / "native" / "events.jsonl", limit=None)
    trace_view = build_trace_view(otel_events, native_events)
    return build_evidence_sources(
        run,
        verifier,
        _visible_changed_files(changes),
        telemetry,
        trace_view,
        workspace_observed=(evidence / "workspace" / "changes.json").exists(),
    )


def _duration_label(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "unknown"
    return f"{seconds:.2f} 秒"


_RUN_STATUS_LABELS = {
    "PENDING": "等待运行",
    "RUNNING": "运行中",
    "COMPLETED": "已完成",
    "ERROR": "运行错误",
}


def _status_label(value: Any) -> str:
    normalized = str(value or "UNKNOWN").upper()
    return _RUN_STATUS_LABELS.get(normalized, normalized)


def _trajectory_for_run(run: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not run:
        return []
    evidence = Path(run["run_dir"])
    verifier = _read_json(evidence / "verifier" / "result.json")
    changes = _read_json(evidence / "workspace" / "changes.json")
    return build_trajectory(
        _read_jsonl(evidence / "telemetry" / "otel" / "events.jsonl", limit=None),
        _read_jsonl(evidence / "native" / "events.jsonl", limit=None),
        verifier=verifier,
        changed_files=_visible_changed_files(changes),
    )


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


def _read_jsonl(path: Path, limit: int | None = 40) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if limit is not None:
        lines = lines[:limit]
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result
