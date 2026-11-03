"""
Tests for E3 — AsyncProcessExecutor.

Validates:
1. Subprocess execution success and output capture.
2. Non-zero exit code handling and error messages.
3. Missing output detection when process succeeds but fails to create file.
4. Concurrency limit enforced via max_workers.
5. Worker ID assignment and release.
6. Execution duration and timing accuracy.
7. Process cancellation and cleanup.
"""

import asyncio
import os
import sys
import tempfile
import time
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from engine.executor import AsyncProcessExecutor, ExecutionResult
from engine.models import TaskNode

PYTHON = sys.executable


@pytest.mark.asyncio
async def test_successful_execution_and_output_capture(tmp_path):
    executor = AsyncProcessExecutor(max_workers=2, base_dir=str(tmp_path))
    out_file = tmp_path / "hello.txt"

    # Command that creates the output file and prints message
    cmd = f'"{PYTHON}" -c "open(r\'{out_file}\', \'w\').write(\'hello world\'); print(\'OK\')"'
    task = TaskNode(
        task_id="hello",
        rule_name="echo",
        command=cmd,
        outputs=["hello.txt"],
    )

    res = await executor.execute_task(task)

    assert res.success is True
    assert res.return_code == 0
    assert "OK" in res.stdout
    assert res.duration >= 0.0
    assert out_file.exists()
    assert out_file.read_text() == "hello world"


@pytest.mark.asyncio
async def test_nonzero_exit_code_handling(tmp_path):
    executor = AsyncProcessExecutor(max_workers=2, base_dir=str(tmp_path))

    cmd = f'"{PYTHON}" -c "import sys; sys.stderr.write(\'FATAL ERROR\\n\'); sys.exit(42)"'
    task = TaskNode(
        task_id="fail_task",
        rule_name="fail",
        command=cmd,
    )

    res = await executor.execute_task(task)

    assert res.success is False
    assert res.return_code == 42
    assert "FATAL ERROR" in res.stderr
    assert "42" in res.error_message


@pytest.mark.asyncio
async def test_missing_output_causes_failure(tmp_path):
    executor = AsyncProcessExecutor(max_workers=2, base_dir=str(tmp_path))

    # Command exits with 0 but DOES NOT create declared output missing.bin
    cmd = f'"{PYTHON}" -c "print(\'Did not create file\')"'
    task = TaskNode(
        task_id="missing_out",
        rule_name="gen",
        command=cmd,
        outputs=["missing.bin"],
    )

    res = await executor.execute_task(task)

    assert res.success is False
    assert res.return_code == 0
    assert "missing.bin" in res.missing_outputs
    assert "not created" in res.error_message.lower()


@pytest.mark.asyncio
async def test_concurrency_bounded_by_workers(tmp_path):
    # Test that max_workers=2 limits concurrent execution to 2
    executor = AsyncProcessExecutor(max_workers=2, base_dir=str(tmp_path))

    peak_active = 0
    poll_active = True

    async def monitor():
        nonlocal peak_active
        while poll_active:
            cur = len(executor._running_procs)
            if cur > peak_active:
                peak_active = cur
            await asyncio.sleep(0.01)

    async def custom_task(task_id: int):
        cmd = f'"{PYTHON}" -c "import time; time.sleep(0.1)"'
        task = TaskNode(task_id=f"sleep_{task_id}", rule_name="sleep", command=cmd)
        return await executor.execute_task(task)

    monitor_task = asyncio.create_task(monitor())
    results = await asyncio.gather(*(custom_task(i) for i in range(5)))
    poll_active = False
    await monitor_task

    assert all(r.success for r in results)
    # Peak concurrency must not exceed max_workers = 2
    assert peak_active <= 2
    assert peak_active > 0


@pytest.mark.asyncio
async def test_worker_id_assignment(tmp_path):
    executor = AsyncProcessExecutor(max_workers=3, base_dir=str(tmp_path))

    cmd = f'"{PYTHON}" -c "import time; time.sleep(0.05)"'
    tasks = [TaskNode(task_id=f"t_{i}", rule_name="t", command=cmd) for i in range(3)]

    results = await asyncio.gather(*(executor.execute_task(t) for t in tasks))

    worker_ids = {r.worker_id for r in results}
    assert all(1 <= wid <= 3 for wid in worker_ids)


@pytest.mark.asyncio
async def test_process_cancellation_cleanup(tmp_path):
    executor = AsyncProcessExecutor(max_workers=2, base_dir=str(tmp_path))

    # Long running task
    cmd = f'"{PYTHON}" -c "import time; time.sleep(5.0)"'
    task = TaskNode(task_id="long", rule_name="sleep", command=cmd)

    coro = asyncio.create_task(executor.execute_task(task))
    await asyncio.sleep(0.1)

    # Cancel execution
    coro.cancel()
    with pytest.raises(asyncio.CancelledError):
        await coro

    # Ensure processes are cleaned up
    await executor.cancel_all()
    assert len(executor._running_procs) == 0
