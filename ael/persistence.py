from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cases import CaseSpec, ExperimentSpec, SuiteSpec, VerifierSpec
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
                    name TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL,
                    executable TEXT NOT NULL DEFAULT '',
                    subject_revision TEXT NOT NULL DEFAULT 'UNKNOWN',
                    agent_version TEXT NOT NULL DEFAULT 'UNKNOWN',
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
                CREATE TABLE IF NOT EXISTS case_catalog (
                    id TEXT PRIMARY KEY,
                    source_path TEXT,
                    display_name TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS failure_runs (
                    failure_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY (failure_id, run_id)
                );
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO failure_runs(failure_id, run_id, first_seen_at) "
                "SELECT id, source_run_id, created_at FROM failures"
            )
            self._ensure_variant_columns(db)

    @staticmethod
    def _ensure_variant_columns(db: sqlite3.Connection) -> None:
        existing = {row[1] for row in db.execute("PRAGMA table_info(variants)")}
        columns = {
            "name": "TEXT NOT NULL DEFAULT ''",
            "executable": "TEXT NOT NULL DEFAULT ''",
            "subject_revision": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "agent_version": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        }
        for name, definition in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE variants ADD COLUMN {name} {definition}")

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

    def list_cases(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM cases ORDER BY id, revision")]

    def list_case_catalog(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM case_catalog ORDER BY id")]

    def get_case_catalog(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM case_catalog WHERE id=?", (case_id,)).fetchone()
        return dict(row) if row else None

    def save_case_catalog(
        self,
        case_id: str,
        *,
        source_path: str | None = None,
        display_name: str | None = None,
        notes: str = "",
        status: str = "ACTIVE",
    ) -> None:
        if status not in {"ACTIVE", "ARCHIVED"}:
            raise ValueError(f"Case 目录状态不合法：{status}")
        now = now_iso()
        with self._connect() as db:
            existing = db.execute("SELECT created_at FROM case_catalog WHERE id=?", (case_id,)).fetchone()
            created_at = existing["created_at"] if existing else now
            db.execute(
                """
                INSERT INTO case_catalog(id, source_path, display_name, notes, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  source_path=COALESCE(excluded.source_path, case_catalog.source_path),
                  display_name=excluded.display_name,
                  notes=excluded.notes,
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (
                    case_id,
                    source_path,
                    display_name or case_id,
                    notes,
                    status,
                    created_at,
                    now,
                ),
            )

    def get_case(self, case_id: str, revision: str | None = None) -> CaseSpec | None:
        query = "SELECT * FROM cases WHERE id=?"
        params: tuple[Any, ...] = (case_id,)
        if revision:
            query += " AND revision=?"
            params = (case_id, revision)
        query += " ORDER BY revision DESC LIMIT 1"
        with self._connect() as db:
            row = db.execute(query, params).fetchone()
        if not row:
            return None
        value = dict(row)
        return CaseSpec(
            id=value["id"],
            prompt=value["prompt"],
            fixture_path=Path(value["fixture_path"]),
            verifier=VerifierSpec(**json.loads(value["verifier_json"])),
            timeout_seconds=int(value["timeout_seconds"]),
            constraints=json.loads(value["constraints_json"]),
            source_path=Path(value["source_path"]) if value["source_path"] else None,
            revision=value["revision"],
            fixture_hash=value["fixture_hash"],
        )

    def suite_cases(self, suite_id: str) -> list[CaseSpec]:
        with self._connect() as db:
            row = db.execute("SELECT case_refs_json FROM suites WHERE id=?", (suite_id,)).fetchone()
        if not row:
            return []
        cases: list[CaseSpec] = []
        for reference in json.loads(row["case_refs_json"]):
            if isinstance(reference, dict):
                case = self.get_case(reference["id"], reference.get("revision"))
            else:
                case = self.get_case(reference)
            if case:
                cases.append(case)
        return cases

    def append_suite_case(self, suite_id: str, kind: str, case: CaseSpec) -> None:
        with self._connect() as db:
            row = db.execute("SELECT case_refs_json FROM suites WHERE id=?", (suite_id,)).fetchone()
            raw_refs = json.loads(row["case_refs_json"]) if row else []
            refs = [
                reference if isinstance(reference, dict) else {"id": reference, "revision": None}
                for reference in raw_refs
            ]
            if not any(
                reference.get("id") == case.id
                and reference.get("revision") == case.revision
                for reference in refs
            ):
                refs.append({"id": case.id, "revision": case.revision})
            db.execute(
                "INSERT OR REPLACE INTO suites(id, kind, case_refs_json) VALUES (?, ?, ?)",
                (suite_id, kind, json.dumps(refs)),
            )

    def save_variant(self, variant: AgentVariant) -> None:
        from .hashing import config_hash

        with self._connect() as db:
            db.execute(
                """
                INSERT INTO variants(
                    id, name, agent_id, executable, subject_revision, agent_version,
                    model, provider, model_config_json, harness_config_json,
                    run_mode, observation_profile, fingerprint_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name,
                  agent_id=excluded.agent_id,
                  executable=excluded.executable,
                  subject_revision=excluded.subject_revision,
                  agent_version=excluded.agent_version,
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
                    variant.name,
                    variant.agent_id,
                    variant.executable,
                    variant.subject_revision,
                    variant.agent_version,
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

    @staticmethod
    def _decode_variant(result: dict[str, Any]) -> dict[str, Any]:
        for key in ("model_config_json", "harness_config_json", "fingerprint_json"):
            value = result.pop(key, None)
            result[key.removesuffix("_json")] = json.loads(value) if value else {}
        result.setdefault("name", "")
        result.setdefault("executable", "")
        result.setdefault("subject_revision", "UNKNOWN")
        result.setdefault("agent_version", "UNKNOWN")
        result["configured_model"] = result.get("model")
        result["configured_provider"] = result.get("provider")
        return result

    def get_variant(self, variant_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        return self._decode_variant(dict(row)) if row else None

    def list_variants(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM variants ORDER BY name, id")]
        return [self._decode_variant(row) for row in rows]

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
                (
                    suite.id,
                    suite.kind,
                    json.dumps([{"id": case.id, "revision": case.revision} for case in suite.cases]),
                ),
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
            db.execute(
                "INSERT OR IGNORE INTO failure_runs(failure_id, run_id, first_seen_at) VALUES (?, ?, ?)",
                (failure_id, source_run_id, now),
            )

    def upsert_failure_cluster(
        self,
        *,
        signature: str,
        source_run_id: str,
        details: dict[str, Any],
    ) -> str:
        now = now_iso()
        with self._connect() as db:
            row = db.execute("SELECT * FROM failures WHERE signature=?", (signature,)).fetchone()
            if row is None:
                failure_id = f"failure-{signature[:16]}"
                aggregate = {
                    **details,
                    "source_run_id": source_run_id,
                    "run_ids": [source_run_id],
                    "run_count": 1,
                    "variant_ids": [details.get("variant_id")] if details.get("variant_id") else [],
                    "experiment_ids": [details.get("experiment_id")] if details.get("experiment_id") else [],
                }
                db.execute(
                    """
                    INSERT INTO failures(id, source_run_id, status, signature, details_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        failure_id,
                        source_run_id,
                        FailureStatus.OBSERVED.value,
                        signature,
                        json.dumps(redact(aggregate), sort_keys=True, default=str),
                        now,
                        now,
                    ),
                )
            else:
                failure_id = str(row["id"])
                aggregate = json.loads(row["details_json"])
                run_ids = list(aggregate.get("run_ids") or [])
                if source_run_id not in run_ids:
                    run_ids.append(source_run_id)
                variant_ids = list(aggregate.get("variant_ids") or [])
                if details.get("variant_id") and details["variant_id"] not in variant_ids:
                    variant_ids.append(details["variant_id"])
                experiment_ids = list(aggregate.get("experiment_ids") or [])
                if details.get("experiment_id") and details["experiment_id"] not in experiment_ids:
                    experiment_ids.append(details["experiment_id"])
                aggregate.update(
                    {
                        "run_ids": run_ids,
                        "run_count": len(run_ids),
                        "variant_ids": variant_ids,
                        "experiment_ids": experiment_ids,
                        "latest_run_id": source_run_id,
                    }
                )
                status = row["status"]
                if status == FailureStatus.OBSERVED.value and len(run_ids) >= 2:
                    status = FailureStatus.REPRODUCED.value
                db.execute(
                    "UPDATE failures SET status=?, details_json=?, updated_at=? WHERE id=?",
                    (status, json.dumps(redact(aggregate), sort_keys=True, default=str), now, failure_id),
                )
            db.execute(
                "INSERT OR IGNORE INTO failure_runs(failure_id, run_id, first_seen_at) VALUES (?, ?, ?)",
                (failure_id, source_run_id, now),
            )
        return failure_id

    def get_failure_by_signature(self, signature: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM failures WHERE signature=?", (signature,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        self._enrich_failure_details(result["details"], [result["source_run_id"]])
        return result

    def failure_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT f.* FROM failures f
                JOIN failure_runs fr ON fr.failure_id=f.id
                WHERE fr.run_id=?
                ORDER BY f.created_at DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        self._enrich_failure_details(result["details"], [run_id])
        return result

    def _enrich_failure_details(self, details: dict[str, Any], run_ids: list[str]) -> None:
        if not run_ids:
            return
        placeholders = ",".join("?" for _ in run_ids)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id, experiment_id, case_id, variant_id FROM runs WHERE id IN ({placeholders})",
                tuple(run_ids),
            ).fetchall()
        if not rows:
            return
        details.setdefault("case_id", rows[0]["case_id"])
        details.setdefault("experiment_id", rows[0]["experiment_id"])
        details.setdefault("variant_id", rows[0]["variant_id"])
        if not details.get("variant_ids"):
            details["variant_ids"] = sorted({row["variant_id"] for row in rows})
        if not details.get("experiment_ids"):
            details["experiment_ids"] = sorted({row["experiment_id"] for row in rows})
        details.setdefault("run_ids", list(run_ids))
        details.setdefault("run_count", len(run_ids))

    def get_failure(self, failure_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM failures WHERE id=?", (failure_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        with self._connect() as db:
            result["run_ids"] = [
                item["run_id"]
                for item in db.execute(
                    "SELECT run_id FROM failure_runs WHERE failure_id=? ORDER BY first_seen_at, run_id",
                    (failure_id,),
                )
            ]
        self._enrich_failure_details(result["details"], result["run_ids"])
        result["details"].setdefault("run_ids", result["run_ids"])
        result["details"].setdefault("run_count", len(result["run_ids"]))
        return result

    def list_failures(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM failures ORDER BY created_at DESC")]
        for result in rows:
            result["details"] = json.loads(result.pop("details_json"))
            with self._connect() as db:
                run_ids = [
                    item["run_id"]
                    for item in db.execute(
                        "SELECT run_id FROM failure_runs WHERE failure_id=? ORDER BY first_seen_at, run_id",
                        (result["id"],),
                    )
                ]
            result["run_ids"] = run_ids
            self._enrich_failure_details(result["details"], run_ids)
            result["details"].setdefault("run_ids", run_ids)
            result["details"].setdefault("run_count", len(run_ids))
        return rows

    def update_failure_status(self, failure_id: str, status: FailureStatus) -> None:
        with self._connect() as db:
            db.execute("UPDATE failures SET status=?, updated_at=? WHERE id=?", (status.value, now_iso(), failure_id))

    def update_failure_details(self, failure_id: str, details: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE failures SET details_json=?, updated_at=? WHERE id=?",
                (json.dumps(redact(details), sort_keys=True, default=str), now_iso(), failure_id),
            )

    def evidence_dir(self, run_id: str) -> Path:
        path = self.runs_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def read_experiment_definition(self, experiment_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT definition_json FROM experiments WHERE id=?",
                (experiment_id,),
            ).fetchone()
        return json.loads(row["definition_json"]) if row else None
