from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .cases import CaseSpec
from .process import ProcessSupervisor
from .redaction import redact


@dataclass(frozen=True)
class VerifierResult:
    outcome: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "stdout": redact(self.stdout),
            "stderr": redact(self.stderr),
            "duration_seconds": self.duration_seconds,
            "error": redact(self.error),
        }


async def run_verifier(
    case: CaseSpec,
    workspace: Path,
    evidence_dir: Path,
    supervisor: ProcessSupervisor,
):
    if case.verifier.command:
        argv = ["/bin/sh", "-lc", case.verifier.command]
    elif case.verifier.python:
        grader = Path(case.verifier.python)
        if not grader.is_absolute() and case.source_path:
            grader = case.source_path.parent / grader
        argv = [sys.executable, str(grader)]
    else:
        return VerifierResult("ERROR", None, "", "", 0.0, "no verifier configured")
    start = time.monotonic()
    process = await supervisor.run(
        argv,
        cwd=workspace,
        env={**os.environ, "AEL_WORKSPACE": str(workspace)},
        timeout_seconds=case.timeout_seconds,
    )
    duration = time.monotonic() - start
    if process.timed_out:
        outcome, error = "ERROR", "verifier timeout"
    elif process.error:
        outcome, error = "ERROR", process.error
    elif process.returncode == 0:
        outcome, error = "PASS", None
    else:
        outcome, error = "FAIL", None
    result = VerifierResult(outcome, process.returncode, process.stdout, process.stderr, duration, error)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "stdout.log").write_text(redact(result.stdout), encoding="utf-8")
    (evidence_dir / "stderr.log").write_text(redact(result.stderr), encoding="utf-8")
    (evidence_dir / "result.json").write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    return result

