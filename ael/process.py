from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from .models import ProcessResult


class ProcessSupervisor:
    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
        stdin: str | None = None,
    ) -> ProcessResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return ProcessResult(None, "", "", error=str(exc))
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode("utf-8") if stdin is not None else None),
                timeout=max(1, timeout_seconds),
            )
        except asyncio.TimeoutError:
            self._terminate(process)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1)
            except asyncio.TimeoutError:
                self._kill(process)
                stdout, stderr = await process.communicate()
            return ProcessResult(
                process.returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                timed_out=True,
            )
        except asyncio.CancelledError:
            self._terminate(process)
            try:
                await asyncio.wait_for(process.communicate(), timeout=1)
            except asyncio.TimeoutError:
                self._kill(process)
            return ProcessResult(None, "", "", cancelled=True)
        return ProcessResult(
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            process.kill()

    @staticmethod
    def _kill(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            process.kill()
