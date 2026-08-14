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
    for run in runs:
        groups[(run["case_id"], run["variant_id"])].append(run["task_outcome"])
    rows = []
    for (case_id, variant_id), outcomes in sorted(groups.items()):
        summary = trial_summary(outcomes)
        rows.append({"case_id": case_id, "variant_id": variant_id, **summary})
    return {"runs": len(runs), "rows": rows}

