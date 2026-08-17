from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any

from .cases import CaseSpec, ExperimentSpec
from .drivers.base import AgentDriver
from .hashing import UNKNOWN, config_hash, git_sha, runtime_profile
from .models import AgentVariant, DriverResult, ObservableEvent, ObservationProfile, RunContext, RunStatus, TaskOutcome
from .persistence import Repository
from .process import ProcessSupervisor
from .redaction import redact
from .observation import filter_event_data, filter_jsonl
from .otel_ingest import ingest_collector_output
from .otel_lifecycle import AELLifecycle
from .verifier import VerifierResult, run_verifier
from .workspace import WorkspaceManager


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_coverage(
    outcome: TaskOutcome,
    changes: dict[str, Any] | None,
    events: list[ObservableEvent],
    usage: dict[str, Any],
) -> dict[str, str]:
    return {
        "结果": "✓" if outcome != TaskOutcome.UNKNOWN else "✗",
        "工作区": "✓" if changes is not None else "✗",
        "工具时间线": "✓" if any(event.kind in {"tool_call", "tool_result", "command"} for event in events) else "?",
        "Token 用量": "✓" if usage else "?",
        "Model 调用": "✓" if any(event.source == "otel" for event in events) else "?",
        "Compaction 事件": "✓" if any("compact" in (event.name or "").lower() for event in events) else "?",
        "Subagent 生命周期": "✓" if any("subagent" in (event.name or "").lower() for event in events) else "?",
    }


class Runner:
    def __init__(self, repository: Repository, drivers: dict[str, AgentDriver], *, supervisor: ProcessSupervisor | None = None):
        self.repository = repository
        self.drivers = drivers
        self.supervisor = supervisor or ProcessSupervisor()
        self.workspaces = WorkspaceManager(repository.ael_dir)

    def _fingerprint(self, experiment: ExperimentSpec, case: CaseSpec, variant: AgentVariant, trial: int, driver: AgentDriver) -> dict[str, Any]:
        agent = driver.agent()
        return {
            "ael_version": "0.1.0",
            "git_sha": git_sha(self.repository.root),
            "variant_name": variant.name or variant.id,
            "agent_id": agent.id,
            "agent_version": variant.agent_version if variant.agent_version not in {"", "UNKNOWN"} else agent.detected_version or UNKNOWN,
            "driver": agent.driver,
            "driver_version": agent.detected_version or UNKNOWN,
            "executable": variant.executable or agent.binary or UNKNOWN,
            "subject_revision": variant.subject_revision or UNKNOWN,
            "model": variant.model,
            "configured_model": variant.model,
            "provider": variant.provider,
            "configured_provider": variant.provider,
            "model_config": redact(variant.model_config),
            "harness_config_hash": config_hash(variant.harness_config),
            "arguments": list(variant.arguments),
            "prompt_transport": variant.prompt_transport,
            "env_delta": redact(variant.env_delta),
            "version_command": list(variant.version_command),
            "run_mode": variant.run_mode.value,
            "observation_profile": variant.observation_profile.value,
            "case_id": case.id,
            "case_revision": case.revision,
            "prompt_hash": case.prompt_hash,
            "fixture_hash": case.fixture_hash,
            "runtime": runtime_profile(),
            "relevant_limits": {"timeout_seconds": case.timeout_seconds},
            "trial": trial,
            "sandbox": variant.harness_config.get("sandbox", UNKNOWN),
            "approval_policy": variant.harness_config.get("approval_policy", UNKNOWN),
            "network_policy": variant.harness_config.get("network_policy", UNKNOWN),
        }

    async def run_case(self, experiment: ExperimentSpec, case: CaseSpec, variant: AgentVariant, trial: int) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        evidence_dir = self.repository.evidence_dir(run_id)
        driver = self.drivers.get(variant.agent_id)
        if driver is None:
            raise ValueError(f"未注册 driver：{variant.agent_id}")
        fingerprint = self._fingerprint(experiment, case, variant, trial, driver)
        self.repository.create_run(run_id, experiment, case, variant, trial, fingerprint, evidence_dir)
        lifecycle = AELLifecycle(
            endpoint=(os.environ.get("AEL_OTEL_ENDPOINT") if variant.observation_profile in {ObservationProfile.TELEMETRY, ObservationProfile.DEEP} else None),
            resource={
                "ael.experiment.id": experiment.id,
                "ael.run.id": run_id,
                "ael.case.id": case.id,
                "ael.variant.id": variant.id,
                "ael.trial": trial,
            },
        )
        run_span = lifecycle.start("ael.run", attributes={"ael.lifecycle.phase": "run"})
        prepare_span = lifecycle.start("workspace.prepare", parent_span_id=run_span)
        try:
            workspace, before = self.workspaces.create(case.fixture_path, run_id)
            lifecycle.end(prepare_span, attributes={"ael.workspace.ready": True})
        except Exception as exc:
            lifecycle.end(prepare_span, error=f"{type(exc).__name__}: {exc}")
            lifecycle.end(run_span, error=f"{type(exc).__name__}: {exc}")
            lifecycle.export()
            raise
        env = dict(os.environ)
        env.update(
            {
                "AEL_EXPERIMENT_ID": experiment.id,
                "AEL_RUN_ID": run_id,
                "AEL_CASE_ID": case.id,
                "AEL_VARIANT_ID": variant.id,
                "AEL_TRIAL": str(trial),
            }
        )
        if variant.observation_profile in {ObservationProfile.TELEMETRY, ObservationProfile.DEEP}:
            env["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(
                f"{key}={value}"
                for key, value in {
                    "ael.experiment.id": experiment.id,
                    "ael.run.id": run_id,
                    "ael.case.id": case.id,
                    "ael.variant.id": variant.id,
                    "ael.variant.subject_revision": variant.subject_revision,
                    "ael.trial": trial,
                }.items()
            )
            endpoint = os.environ.get("AEL_OTEL_ENDPOINT")
            if endpoint:
                env["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
        case_prompt = case.prompt
        if variant.harness_config.get("prompt_intervention") is True:
            case_prompt += (
                "\n\nBefore reporting completion, run the complete test suite, inspect every "
                "failure it reveals, and only then finish the task."
            )
        context = RunContext(
            run_id=run_id,
            experiment_id=experiment.id,
            case_id=case.id,
            variant=variant,
            trial=trial,
            workspace=workspace,
            evidence_dir=evidence_dir,
            timeout_seconds=case.timeout_seconds,
            env=env,
            observation_profile=variant.observation_profile,
            case_prompt=case_prompt,
        )
        result = DriverResult(process_error="driver 未返回结果")
        status = RunStatus.INVALID
        outcome = TaskOutcome.UNKNOWN
        verifier_result = None
        error = None
        changes = None
        capture_error = None
        started = time.monotonic()
        agent_span = lifecycle.start("agent.execute", parent_span_id=run_span)
        try:
            result = await asyncio.wait_for(driver.execute(context, self.supervisor), timeout=max(1, case.timeout_seconds))
            if result.cancelled:
                status = RunStatus.CANCELLED
            elif result.timed_out:
                status = RunStatus.TIMEOUT
            elif result.process_error or result.exit_code not in (0, None):
                status = RunStatus.PROCESS_ERROR
                error = result.process_error or f"Agent exit code={result.exit_code}"
            elif result.exit_code is None:
                status = RunStatus.PROCESS_ERROR
                error = "Agent exit code 不可用"
            else:
                status = RunStatus.COMPLETED
                verifier_span = lifecycle.start("verifier.execute", parent_span_id=run_span)
                try:
                    verifier_result = await run_verifier(case, workspace, evidence_dir / "verifier", self.supervisor)
                    lifecycle.end(
                        verifier_span,
                        attributes={"ael.verifier.outcome": verifier_result.outcome},
                    )
                except Exception as exc:
                    lifecycle.end(verifier_span, error=f"{type(exc).__name__}: {exc}")
                    verifier_result = VerifierResult(
                        "ERROR",
                        None,
                        "",
                        "",
                        time.monotonic() - started,
                        f"{type(exc).__name__}: {exc}",
                    )
                    verifier_dir = evidence_dir / "verifier"
                    verifier_dir.mkdir(parents=True, exist_ok=True)
                    (verifier_dir / "result.json").write_text(
                        json.dumps(verifier_result.to_dict(), indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
                    status = RunStatus.VERIFIER_ERROR
                    outcome = TaskOutcome.UNKNOWN
                    error = verifier_result.error
                if verifier_result.outcome == "PASS":
                    outcome = TaskOutcome.PASS
                elif verifier_result.outcome == "FAIL":
                    outcome = TaskOutcome.FAIL
                else:
                    outcome = TaskOutcome.UNKNOWN
                    status = RunStatus.VERIFIER_ERROR
                    error = verifier_result.error or "verifier 错误"
        except asyncio.TimeoutError:
            status, error, result = RunStatus.TIMEOUT, "Agent 超时", DriverResult(timed_out=True, process_error="Agent 超时")
        except asyncio.CancelledError:
            status, error, result = RunStatus.CANCELLED, "运行已取消", DriverResult(cancelled=True, process_error="运行已取消")
        except Exception as exc:
            status, error, result = RunStatus.PROCESS_ERROR, f"{type(exc).__name__}: {exc}", DriverResult(process_error=f"{type(exc).__name__}: {exc}")
        finally:
            lifecycle.end(
                agent_span,
                error=error if status not in {RunStatus.COMPLETED} else None,
                attributes={"ael.run.status": status.value},
            )
            capture_span = lifecycle.start("workspace.capture", parent_span_id=run_span)
            try:
                changes = self.workspaces.capture(workspace, before, evidence_dir / "workspace")
                lifecycle.end(
                    capture_span,
                    attributes={
                        "ael.workspace.changed_files": len((changes or {}).get("changed_files") or []),
                    },
                )
            except Exception as exc:
                lifecycle.end(capture_span, error=f"{type(exc).__name__}: {exc}")
                capture_error = f"Evidence capture failed: {type(exc).__name__}: {exc}"
            finally:
                self.workspaces.cleanup(workspace)
        if capture_error:
            error = error or capture_error
        otel_events: list[ObservableEvent] = []
        otel_summary: dict[str, Any] = {
            "source": "otel_collector",
            "run_id": run_id,
            "events": 0,
            "evidence": "insufficient evidence",
        }
        lifecycle.end(
            run_span,
            error=error if status not in {RunStatus.COMPLETED} else None,
            attributes={
                "ael.run.status": status.value,
                "ael.task.outcome": outcome.value,
            },
        )
        lifecycle_export = lifecycle.export()
        lifecycle_dir = evidence_dir / "telemetry" / "otel"
        lifecycle_dir.mkdir(parents=True, exist_ok=True)
        (lifecycle_dir / "ael-lifecycle.json").write_text(
            json.dumps(
                {"span_snapshot": lifecycle.span_snapshot(), "export": lifecycle_export},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        otel_summary["ael_lifecycle"] = lifecycle_export
        if variant.observation_profile in {ObservationProfile.TELEMETRY, ObservationProfile.DEEP}:
            if os.environ.get("AEL_OTEL_ENDPOINT"):
                await asyncio.sleep(1.0)
            otel_events, otel_summary = ingest_collector_output(self.repository.root, run_id, evidence_dir)
        otel_summary["ael_lifecycle"] = lifecycle_export
        events = [*result.native_events, *otel_events]
        execution_receipt = result.execution_receipt or {
            "resolved_executable": fingerprint.get("executable", UNKNOWN),
            "argv": [fingerprint.get("executable", UNKNOWN), *list(variant.arguments)],
            "normalized_argv": [fingerprint.get("executable", UNKNOWN), *list(variant.arguments)],
            "cwd": str(workspace),
            "prompt_hash": case.prompt_hash,
            "prompt_transport": variant.prompt_transport,
            "relevant_environment_delta": redact(variant.env_delta),
            "effective_execution_hash": UNKNOWN,
            "evidence": "driver did not expose a resolved execution receipt",
        }
        execution_receipt["execution_source"] = "driver" if result.execution_receipt else "fallback"
        (evidence_dir / "execution-receipt.json").write_text(
            json.dumps(redact(execution_receipt), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        fingerprint["effective_execution_hash"] = execution_receipt.get("effective_execution_hash", UNKNOWN)
        self.repository.update_run_fingerprint(run_id, fingerprint)
        native_dir = evidence_dir / "native"
        native_dir.mkdir(parents=True, exist_ok=True)
        raw_native = filter_jsonl(result.stdout, variant.observation_profile)
        (native_dir / "raw.jsonl").write_text(raw_native, encoding="utf-8")
        (native_dir / "stdout.log").write_text(raw_native, encoding="utf-8")
        (native_dir / "stderr.log").write_text(redact(result.stderr), encoding="utf-8")
        (native_dir / "events.jsonl").write_text(
            "".join(
                json.dumps(filter_event_data(event.to_dict(), variant.observation_profile), sort_keys=True) + "\n"
                for event in events
            ),
            encoding="utf-8",
        )
        telemetry_dir = evidence_dir / "telemetry"
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        telemetry_raw = telemetry_dir / "raw"
        telemetry_raw.mkdir(parents=True, exist_ok=True)
        (telemetry_raw / "events.jsonl").write_text(
            "".join(json.dumps(filter_event_data(event.to_dict(), variant.observation_profile), sort_keys=True) + "\n" for event in events if event.source == "otel"),
            encoding="utf-8",
        )
        (telemetry_dir / "summary.json").write_text(
            json.dumps(
                redact({"native_usage": result.usage, "otel": otel_summary}),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (evidence_dir / "metadata.json").write_text(
            json.dumps(
                redact(
                    {
                        "run_id": run_id,
                        "experiment_id": experiment.id,
                        "case_id": case.id,
                        "variant_id": variant.id,
                        "trial": trial,
                        "status": status.value,
                        "outcome": outcome.value,
                        "duration_seconds": time.monotonic() - started,
                        "fingerprint": fingerprint,
                        "session_id": result.session_id,
                        "final_text": result.final_text if variant.observation_profile == ObservationProfile.DEEP else None,
                        "native_event_count": len(result.native_events),
                        "otel_event_count": len(otel_events),
                        "error": error,
                    }
                ),
                indent=2,
                sort_keys=True,
                default=str,
            ),
            encoding="utf-8",
        )
        coverage = evidence_coverage(outcome, changes, events, result.usage)
        self.repository.finalize_run(
            run_id,
            status=status,
            outcome=outcome,
            coverage=coverage,
            verifier=verifier_result.to_dict() if verifier_result else None,
            error=error,
        )
        failure_id = None
        if status == RunStatus.COMPLETED and outcome == TaskOutcome.FAIL:
            from .failures import observe_failure

            failure_id = observe_failure(self.repository, run_id)
        return {
            "run_id": run_id,
            "experiment_id": experiment.id,
            "case_id": case.id,
            "variant_id": variant.id,
            "trial": trial,
            "status": status.value,
            "outcome": outcome.value,
            "duration_seconds": time.monotonic() - started,
            "coverage": coverage,
            "changes": changes,
            "error": error,
            "evidence_dir": str(evidence_dir),
            "fingerprint": fingerprint,
            "execution_receipt": execution_receipt,
            "failure_id": failure_id,
        }

    async def run_experiment(self, experiment: ExperimentSpec) -> list[dict[str, Any]]:
        stored = self.repository.load_experiment(experiment.id)
        if stored is not None:
            # A Web request may have persisted the definition before the
            # background task starts.  The stored definition is authoritative;
            # it already points at the frozen CaseRevision and Variant snapshot.
            experiment = stored
            self.repository.set_experiment_status(experiment.id, "RUNNING")
        else:
            frozen_cases = tuple(self.repository.freeze_case(case) for case in experiment.suite.cases)
            if frozen_cases != experiment.suite.cases:
                experiment = replace(
                    experiment,
                    suite=replace(experiment.suite, cases=frozen_cases),
                )
            self.repository.save_experiment(experiment, status="RUNNING")
        semaphore = asyncio.Semaphore(experiment.max_concurrency)

        async def one(case: CaseSpec, variant: AgentVariant, trial: int) -> dict[str, Any]:
            async with semaphore:
                return await self.run_case(experiment, case, variant, trial)

        jobs = [
            one(case, variant, trial)
            for variant in experiment.variants
            for case in experiment.suite.cases
            for trial in range(1, experiment.trials + 1)
        ]
        try:
            results = await asyncio.gather(*jobs)
        except Exception:
            self.repository.set_experiment_status(experiment.id, "ERROR")
            raise
        self.repository.set_experiment_status(experiment.id, "COMPLETED")
        source_run_id = experiment.metadata.get("source_run_id")
        if source_run_id:
            from .failures import reconcile_source_experiment

            reconcile_source_experiment(self.repository, experiment.id, str(source_run_id))
        return results
