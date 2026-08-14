from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .persistence import Repository
from .reports import matrix_report


def compare_experiments(repository: Repository, experiment_a: str, experiment_b: str) -> dict[str, Any]:
    runs_a = repository.list_runs(experiment_a)
    runs_b = repository.list_runs(experiment_b)
    return {
        "experiment_a": experiment_a,
        "experiment_b": experiment_b,
        "matrix_a": matrix_report(runs_a),
        "matrix_b": matrix_report(runs_b),
        "differential_cases": [],
        "note": "Detailed differential analysis is added by the Failure Explorer milestone.",
    }


def compare_run_details(repository: Repository, run_id: str) -> dict[str, Any]:
    run = repository.get_run(run_id)
    if not run:
        return {}
    events_path = Path(run["run_dir"]) / "native" / "events.jsonl"
    events = []
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {
        "run_id": run_id,
        "candidate": {"status": run["run_status"], "outcome": run["task_outcome"]},
        "events": events,
        "note": "No matched PASS reference is available in this first view; no trajectory attribution is made.",
    }
