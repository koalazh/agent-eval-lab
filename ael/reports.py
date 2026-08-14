from __future__ import annotations

from collections import defaultdict
from typing import Any

from .models import TaskOutcome


def trial_summary(outcomes: list[str]) -> dict[str, Any]:
    total = len(outcomes)
    passes = sum(item == TaskOutcome.PASS.value for item in outcomes)
    fails = sum(item == TaskOutcome.FAIL.value for item in outcomes)
    unknown = total - passes - fails
    if total == 0 or unknown == total:
        classification = "ERROR"
    elif passes == total:
        classification = "STABLE_PASS"
    elif fails == total:
        classification = "STABLE_FAIL"
    elif passes:
        classification = "FLAKY"
    else:
        classification = "UNKNOWN"
    return {
        "passes": passes,
        "fails": fails,
        "unknown": unknown,
        "total": total,
        "classification": classification,
        "display": f"{passes}/{total} PASS",
    }


def matrix_report(runs: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    run_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        outcome = run.get("task_outcome", run.get("outcome", "UNKNOWN"))
        groups[(run["case_id"], run["variant_id"])].append(outcome)
        run_groups[(run["case_id"], run["variant_id"])].append(run)
    rows = []
    for (case_id, variant_id), outcomes in sorted(groups.items()):
        summary = trial_summary(outcomes)
        rows.append(
            {
                "case_id": case_id,
                "variant_id": variant_id,
                "run_ids": [run["id"] if "id" in run else run.get("run_id") for run in run_groups[(case_id, variant_id)]],
                **summary,
            }
        )

    case_ids = sorted({case_id for case_id, _ in groups})
    variant_ids = sorted({variant_id for _, variant_id in groups})
    matrix_rows = []
    for case_id in case_ids:
        cells = {}
        case_summaries = []
        for variant_id in variant_ids:
            summary = trial_summary(groups.get((case_id, variant_id), []))
            cell_runs = run_groups.get((case_id, variant_id), [])
            target_run = next(
                (
                    run
                    for run in cell_runs
                    if run.get("task_outcome", run.get("outcome")) == TaskOutcome.FAIL.value
                ),
                cell_runs[0] if cell_runs else None,
            )
            summary = {
                **summary,
                "run_ids": [
                    run["id"] if "id" in run else run.get("run_id")
                    for run in cell_runs
                ],
                "target_run_id": (
                    target_run.get("id")
                    if target_run and "id" in target_run
                    else target_run.get("run_id") if target_run else None
                ),
            }
            cells[variant_id] = summary
            if summary["total"]:
                case_summaries.append(summary)
        differential = bool(
            case_summaries
            and any(summary["passes"] for summary in case_summaries)
            and any(summary["fails"] for summary in case_summaries)
        )
        for summary in cells.values():
            summary["differential"] = differential
            summary["label"] = "DIFFERENTIAL" if differential else summary["classification"]
        matrix_rows.append({"case_id": case_id, "cells": cells, "differential": differential})

    passes = sum(run.get("task_outcome", run.get("outcome")) == TaskOutcome.PASS.value for run in runs)
    fails = sum(run.get("task_outcome", run.get("outcome")) == TaskOutcome.FAIL.value for run in runs)
    return {
        "runs": len(runs),
        "rows": rows,
        "case_ids": case_ids,
        "variant_ids": variant_ids,
        "matrix_rows": matrix_rows,
        "summary": {
            "passes": passes,
            "fails": fails,
            "unknown": len(runs) - passes - fails,
            "pass_rate": f"{passes}/{len(runs)}" if runs else "0/0",
        },
    }
