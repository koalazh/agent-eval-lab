from __future__ import annotations

import sys

import pytest

from ael.process import ProcessSupervisor


@pytest.mark.asyncio
async def test_process_supervisor_kills_timed_out_process_group(tmp_path):
    result = await ProcessSupervisor().run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_seconds=1,
    )
    assert result.timed_out is True
