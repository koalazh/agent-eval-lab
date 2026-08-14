from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cases import CaseSpec, ExperimentSpec, SuiteSpec
from .models import Agent, AgentVariant, FailureStatus, RunStatus, TaskOutcome
from .redaction import redact


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.ael_dir = self.root / ".ael"
        self.runs_dir = self.ael_dir / "runs"
        self.failures_dir = self.ael_dir / "failures"
        self.db_path = self.ael_dir / "ael.db"
        for path in (self.ael_dir, self.runs_dir, self.failures_dir):
            path.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    driver TEXT NOT NULL,
                    binary TEXT NOT NULL,
                    detected_version TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS variants (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_config_json TEXT NOT NULL,
                    harness_config_json TEXT NOT NULL,
                    run_mode TEXT NOT NULL,
                    observation_profile TEXT NOT NULL,
                    fingerprint_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    source_path TEXT,
                    fixture_path TEXT NOT NULL,
                    fixture_hash TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    verifier_json TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    constraints_json TEXT NOT NULL,
                    PRIMARY KEY (id, revision)
                );
                CREATE TABLE IF NOT EXISTS suites (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    case_refs_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY,
                    suite_id TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    trials INTEGER NOT NULL,
                    max_concurrency INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    follow_up_of TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    case_revision TEXT NOT NULL,
                    variant_id TEXT NOT NULL,
                    trial INTEGER NOT NULL,
                    run_status TEXT NOT NULL,
                    task_outcome TEXT NOT NULL,
                    run_dir TEXT NOT NULL,
                    fingerprint_json TEXT NOT NULL,
                    evidence_coverage_json TEXT NOT NULL,
                    verifier_json TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS failures (
                    id TEXT PRIMARY KEY,
                    source_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def save_agent(self, agent: Agent) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO agents(id, display_name, driver, binary, detected_version, capabilities_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  display_name=excluded.display_name,
                  driver=excluded.driver,
                  binary=excluded.binary,
                  detected_version=excluded.detected_version,
                  capabilities_json=excluded.capabilities_json
                """,
                (
                    agent.id,
                    agent.display_name,
                    agent.driver,
                    agent.binary,
                    agent.detected_version,
                    json.dumps(redact(agent.capabilities), sort_keys=True),
                ),
            )

    def list_agents(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM agents ORDER BY id")]

    def save_variant(self, variant: AgentVariant) -> None:
        from .hashing import config_hash

        with self._connect() as db:
            db.execute(
                """
                INSERT INTO variants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  agent_id=excluded.agent_id,
                  model=excluded.model,
                  provider=excluded.provider,
                  model_config_json=excluded.model_config_json,
                  harness_config_json=excluded.harness_config_json,
                  run_mode=excluded.run_mode,
                  observation_profile=excluded.observation_profile,
                  fingerprint_json=excluded.fingerprint_json
                """,
                (
                    variant.id,
                    variant.agent_id,
                    variant.model,
                    variant.provider,
                    json.dumps(redact(variant.model_config), sort_keys=True),
                    json.dumps(redact(variant.harness_config), sort_keys=True),
                    variant.run_mode.value,
                    variant.observation_profile.value,
                    json.dumps(
                        {
                            "model_config_hash": config_hash(variant.model_config),
                            "harness_config_hash": config_hash(variant.harness_config),
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def save_case(self, case: CaseSpec) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case.id,
                    case.revision,
                    str(case.source_path) if case.source_path else None,
                    str(case.fixture_path),
                    case.fixture_hash,
                    case.prompt,
                    json.dumps(case.verifier.to_dict(), sort_keys=True),
                    case.timeout_seconds,
                    json.dumps(redact(case.constraints), sort_keys=True),
                ),
            )

    def save_suite(self, suite: SuiteSpec) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO suites VALUES (?, ?, ?)",
                (suite.id, suite.kind, json.dumps([case.id for case in suite.cases])),
            )
        for case in suite.cases:
            self.save_case(case)

    def save_experiment(self, experiment: ExperimentSpec, status: str = "PENDING", follow_up_of: str | None = None) -> None:
        now = now_iso()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO experiments(id, suite_id, definition_json, trials, max_concurrency, status, follow_up_of, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  suite_id=excluded.suite_id,
                  definition_json=excluded.definition_json,
                  trials=excluded.trials,
                  max_concurrency=excluded.max_concurrency,
                  status=excluded.status,
                  follow_up_of=COALESCE(excluded.follow_up_of, experiments.follow_up_of),
                  updated_at=excluded.updated_at
                """,
                (
                    experiment.id,
                    experiment.suite.id,
                    json.dumps(redact(experiment.to_dict()), sort_keys=True, default=str),
                    experiment.trials,
                    experiment.max_concurrency,
                    status,
                    follow_up_of,
                    now,
                    now,
                ),
            )
        self.save_suite(experiment.suite)
        for variant in experiment.variants:
            self.save_variant(variant)

    def set_experiment_status(self, experiment_id: str, status: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE experiments SET status=?, updated_at=? WHERE id=?", (status, now_iso(), experiment_id))

    def list_experiments(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM experiments ORDER BY created_at DESC")]

    def create_run(
        self,
        run_id: str,
        experiment: ExperimentSpec,
        case: CaseSpec,
        variant: AgentVariant,
        trial: int,
        fingerprint: dict[str, Any],
        run_dir: Path,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO runs(
                  id, experiment_id, case_id, case_revision, variant_id, trial,
                  run_status, task_outcome, run_dir, fingerprint_json,
                  evidence_coverage_json, verifier_json, started_at, finished_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    experiment.id,
                    case.id,
                    case.revision,
                    variant.id,
                    trial,
                    RunStatus.INVALID.value,
                    TaskOutcome.UNKNOWN.value,
                    str(run_dir),
                    json.dumps(redact(fingerprint), sort_keys=True, default=str),
                    "{}",
                    None,
                    now_iso(),
                    None,
                    None,
                ),
            )

    def finalize_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        outcome: TaskOutcome,
        coverage: dict[str, str],
        verifier: dict[str, Any] | None,
        error: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE runs SET run_status=?, task_outcome=?, evidence_coverage_json=?,
                    verifier_json=?, finished_at=?, error=? WHERE id=?
                """,
                (
                    status.value,
                    outcome.value,
                    json.dumps(redact(coverage), sort_keys=True),
                    json.dumps(redact(verifier), sort_keys=True) if verifier else None,
                    now_iso(),
                    redact(error),
                    run_id,
                ),
            )

    @staticmethod
    def _decode_run(result: dict[str, Any]) -> dict[str, Any]:
        for key in ("fingerprint_json", "evidence_coverage_json", "verifier_json"):
            if result.get(key):
                result[key.removesuffix("_json")] = json.loads(result[key])
        return result

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._decode_run(dict(row)) if row else None

    def list_runs(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM runs"
        params: tuple[Any, ...] = ()
        if experiment_id:
            query += " WHERE experiment_id=?"
            params = (experiment_id,)
        query += " ORDER BY started_at, id"
        with self._connect() as db:
            rows = [dict(row) for row in db.execute(query, params)]
        return [self._decode_run(row) for row in rows]

    def save_failure(self, failure_id: str, source_run_id: str, signature: str, details: dict[str, Any]) -> None:
        now = now_iso()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO failures(id, source_run_id, status, signature, details_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  signature=excluded.signature,
                  details_json=excluded.details_json,
                  updated_at=excluded.updated_at
                """,
                (
                    failure_id,
                    source_run_id,
                    FailureStatus.OBSERVED.value,
                    signature,
                    json.dumps(redact(details), sort_keys=True, default=str),
                    now,
                    now,
                ),
            )

    def get_failure(self, failure_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM failures WHERE id=?", (failure_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    def list_failures(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM failures ORDER BY created_at DESC")]
        for result in rows:
            result["details"] = json.loads(result.pop("details_json"))
        return rows

    def update_failure_status(self, failure_id: str, status: FailureStatus) -> None:
        with self._connect() as db:
            db.execute("UPDATE failures SET status=?, updated_at=? WHERE id=?", (status.value, now_iso(), failure_id))

    def evidence_dir(self, run_id: str) -> Path:
        path = self.runs_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

