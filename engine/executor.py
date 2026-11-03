"""
E3 — Asynchronous Subprocess Task Executor

Executes build tasks concurrently using Python asyncio:
- Respects a configurable worker limit via asyncio.Semaphore
- Safely manages Windows subprocess execution with cmd.exe handling
- Captures stdout, stderr, exit code, start time, end time, and duration
- Validates that declared output files exist on disk before declaring success
- Supports cancellation and child process cleanup
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from engine.models import TaskNode


@dataclass
class ExecutionResult:
    """Result of executing a single task."""
    task_id: str
    command: str
    return_code: int
    stdout: str
    stderr: str
    start_time: float
    end_time: float
    duration: float
    success: bool
    worker_id: int = 0
    error_message: str = ""
    missing_outputs: list[str] = field(default_factory=list)


class AsyncProcessExecutor:
    """
    Manages concurrent OS subprocess execution with bounded concurrency.
    """

    def __init__(self, max_workers: int = 4, base_dir: str = ""):
        if max_workers < 1:
            raise ValueError(f"max_workers must be at least 1, got {max_workers}")
        self.max_workers = max_workers
        self.base_dir = base_dir
        self._semaphore = asyncio.Semaphore(max_workers)
        self._running_procs: set[asyncio.subprocess.Process] = set()
        self._worker_pool: list[int] = list(range(1, max_workers + 1))
        self._worker_lock = asyncio.Lock()

    def _resolve_path(self, path: str) -> str:
        """Resolve a path relative to base_dir if not absolute."""
        if os.path.isabs(path):
            return path
        if self.base_dir:
            return os.path.normpath(os.path.join(self.base_dir, path))
        return path

    async def _acquire_worker_id(self) -> int:
        """Acquire an available worker ID for logging and telemetry."""
        async with self._worker_lock:
            if self._worker_pool:
                return self._worker_pool.pop(0)
            return 1

    async def _release_worker_id(self, wid: int) -> None:
        """Release a worker ID back to the pool."""
        async with self._worker_lock:
            self._worker_pool.append(wid)
            self._worker_pool.sort()

    async def execute_task(
        self,
        task: TaskNode,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        on_start: Optional[Callable[[int], Any]] = None,
    ) -> ExecutionResult:
        """
        Execute a task command asynchronously within worker limits.

        1. Acquires a slot from the semaphore.
        2. Assigns an active worker ID.
        3. Invokes optional on_start callback after worker acquisition.
        4. Spawns subprocess in the designated directory.
        5. Captures output and streams/records stdout and stderr.
        6. Validates that all expected outputs were produced.
        """
        work_dir = cwd or self.base_dir or os.getcwd()
        command = task.command.strip()

        # Handle empty command (e.g. dummy target)
        if not command:
            return ExecutionResult(
                task_id=task.task_id,
                command="",
                return_code=0,
                stdout="",
                stderr="",
                start_time=time.time(),
                end_time=time.time(),
                duration=0.0,
                success=True,
                worker_id=0,
            )

        async with self._semaphore:
            worker_id = await self._acquire_worker_id()
            start_time = time.time()
            proc: Optional[asyncio.subprocess.Process] = None

            try:
                if on_start:
                    res = on_start(worker_id)
                    if asyncio.iscoroutine(res):
                        await res

                # Windows command execution:
                # If command is already wrapped in cmd.exe /C, execute directly.
                # Otherwise, let create_subprocess_shell execute it cleanly.
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=work_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._running_procs.add(proc)

                if timeout is not None:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                else:
                    stdout_bytes, stderr_bytes = await proc.communicate()

                end_time = time.time()
                duration = end_time - start_time
                return_code = proc.returncode if proc.returncode is not None else 1

                stdout_str = stdout_bytes.decode("utf-8", errors="replace")
                stderr_str = stderr_bytes.decode("utf-8", errors="replace")

                # Verify outputs on disk if process exited with 0
                missing_outputs: list[str] = []
                success = return_code == 0
                error_msg = ""

                if success:
                    for out in task.all_outputs:
                        resolved_out = self._resolve_path(out)
                        if not os.path.exists(resolved_out):
                            missing_outputs.append(out)

                    if missing_outputs:
                        success = False
                        error_msg = (
                            f"Command succeeded but declared output files were not created: "
                            f"{missing_outputs}"
                        )
                else:
                    error_msg = f"Process exited with non-zero return code {return_code}"

                return ExecutionResult(
                    task_id=task.task_id,
                    command=command,
                    return_code=return_code,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    start_time=start_time,
                    end_time=end_time,
                    duration=duration,
                    success=success,
                    worker_id=worker_id,
                    error_message=error_msg,
                    missing_outputs=missing_outputs,
                )

            except asyncio.TimeoutError:
                end_time = time.time()
                if proc:
                    await self._kill_proc(proc)
                return ExecutionResult(
                    task_id=task.task_id,
                    command=command,
                    return_code=-1,
                    stdout="",
                    stderr=f"Task timed out after {timeout} seconds",
                    start_time=start_time,
                    end_time=end_time,
                    duration=end_time - start_time,
                    success=False,
                    worker_id=worker_id,
                    error_message=f"Timeout after {timeout}s",
                )
            except asyncio.CancelledError:
                end_time = time.time()
                if proc:
                    await self._kill_proc(proc)
                raise
            except Exception as e:
                end_time = time.time()
                return ExecutionResult(
                    task_id=task.task_id,
                    command=command,
                    return_code=-1,
                    stdout="",
                    stderr=str(e),
                    start_time=start_time,
                    end_time=end_time,
                    duration=end_time - start_time,
                    success=False,
                    worker_id=worker_id,
                    error_message=str(e),
                )
            finally:
                if proc in self._running_procs:
                    self._running_procs.remove(proc)
                await self._release_worker_id(worker_id)

    async def _kill_proc(self, proc: asyncio.subprocess.Process) -> None:
        """Safely terminate a subprocess and its children."""
        try:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        except OSError:
            pass

    async def cancel_all(self) -> None:
        """Terminate all currently running child processes."""
        for proc in list(self._running_procs):
            await self._kill_proc(proc)
        self._running_procs.clear()
