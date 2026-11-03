"""
E3 — Topological Scheduler & Parallel Task Orchestrator

Schedules build tasks according to the dependency graph:
- In-degree and readiness tracking
- Bounded concurrency with worker limit
- Tasks with in-degree 0 are ready to execute
- Up-to-date tasks (SKIP from E2) do not consume worker slots and immediately unblock dependents
- Rebuild tasks (BUILD from E2) execute concurrently via AsyncProcessExecutor
- On success: E2's record_success() is invoked to persist trace, unblocking dependents
- On failure: downstream dependents are marked BLOCKED
- Independent branches continue executing safely
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Union

from engine.dag import BuildGraph
from engine.executor import AsyncProcessExecutor, ExecutionResult
from engine.models import TaskNode
from engine.rebuilder import RebuildAction, Rebuilder


class TaskStatus(Enum):
    """Execution status of a task in the build engine."""
    PENDING = "PENDING"      # Waiting on prerequisites
    READY = "READY"          # All prerequisites completed/skipped; ready to execute
    RUNNING = "RUNNING"      # Executing on an active worker
    COMPLETED = "COMPLETED"  # Executed successfully, outputs validated, trace recorded
    SKIPPED = "SKIPPED"      # Up to date according to E2, bypassed without running command
    FAILED = "FAILED"        # Command failed (non-zero exit) or expected outputs missing
    BLOCKED = "BLOCKED"      # Cannot execute because a prerequisite failed


@dataclass
class TaskRecord:
    """Tracks state and telemetry for a single task in a build session."""
    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    decision_action: Optional[str] = None
    decision_reason: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    duration: float = 0.0
    worker_id: int = 0
    return_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""


@dataclass
class BuildResult:
    """Summary of the complete build execution."""
    target: str
    success: bool
    total_tasks: int
    completed_count: int
    skipped_count: int
    failed_count: int
    blocked_count: int
    start_time: float
    end_time: float
    duration: float
    max_workers: int
    peak_concurrency: int
    task_records: dict[str, TaskRecord] = field(default_factory=dict)

    def summary(self) -> str:
        status_str = "SUCCESS" if self.success else "FAILED"
        lines = [
            "=" * 60,
            f"Build Summary - {status_str} (Target: {self.target})",
            "=" * 60,
            f"  Total Duration:     {self.duration:.3f} s",
            f"  Workers Configured: {self.max_workers} (Peak concurrency: {self.peak_concurrency})",
            f"  Total Tasks:        {self.total_tasks}",
            f"    - Completed:      {self.completed_count}",
            f"    - Skipped:        {self.skipped_count}",
            f"    - Failed:         {self.failed_count}",
            f"    - Blocked:        {self.blocked_count}",
            "=" * 60,
        ]
        return "\n".join(lines)


class TopologicalScheduler:
    """
    Dependency-aware topological scheduler.
    """

    def __init__(
        self,
        graph: BuildGraph,
        rebuilder: Rebuilder,
        executor: Optional[AsyncProcessExecutor] = None,
        max_workers: int = 4,
        on_event: Optional[Callable[[str, TaskRecord], None]] = None,
    ):
        self.graph = graph
        self.rebuilder = rebuilder
        self.max_workers = max_workers
        self.executor = executor or AsyncProcessExecutor(
            max_workers=max_workers, base_dir=graph.base_dir
        )
        self.on_event = on_event

        # State tracking
        self.task_records: dict[str, TaskRecord] = {}
        self.in_degree: dict[str, int] = {}
        self._current_running: int = 0
        self._peak_concurrency: int = 0
        self._lock = asyncio.Lock()

    def _notify(self, event_type: str, record: TaskRecord) -> None:
        """Deliver notification callback if registered."""
        if self.on_event:
            try:
                self.on_event(event_type, record)
            except Exception:
                pass

    def _block_downstream(self, failed_tid: str, closure: set[str]) -> None:
        """
        Recursively mark all downstream dependents in closure as BLOCKED.
        """
        queue = list(self.graph.nodes[failed_tid].dependents & closure)
        while queue:
            tid = queue.pop(0)
            record = self.task_records[tid]
            if record.status in (TaskStatus.PENDING, TaskStatus.READY):
                record.status = TaskStatus.BLOCKED
                record.error_message = f"Blocked by failure of prerequisite task."
                self._notify("TASK_BLOCKED", record)
                # Propagate further downstream
                for next_tid in self.graph.nodes[tid].dependents & closure:
                    if next_tid not in queue:
                        queue.append(next_tid)

    async def build(
        self,
        target: Optional[Union[str, list[str]]] = None,
    ) -> BuildResult:
        """
        Execute build for the given target(s) using topological scheduling.
        """
        # Validate that graph is acyclic before attempting to schedule
        self.graph.detect_cycles()

        # 1. Identify target closure
        closure = self.graph.get_target_closure(target)
        target_name = (
            ", ".join(target)
            if isinstance(target, list)
            else (target or ", ".join(self.graph.defaults) or "all")
        )

        build_start = time.time()
        self._current_running = 0
        self._peak_concurrency = 0

        # Initialize task records and in-degrees
        # in-degree counts ALL dependencies (explicit, implicit, and order-only) within closure
        self.task_records = {tid: TaskRecord(task_id=tid) for tid in closure}
        self.in_degree = {}
        for tid in closure:
            task = self.graph.nodes[tid]
            prereqs_in_closure = [d for d in task.dependencies if d in closure]
            self.in_degree[tid] = len(prereqs_in_closure)

        ready_queue: asyncio.Queue[str] = asyncio.Queue()

        # Seed initial ready queue (in-degree 0)
        initial_ready = [tid for tid in closure if self.in_degree[tid] == 0]
        initial_ready.sort()
        for tid in initial_ready:
            self.task_records[tid].status = TaskStatus.READY
            self._notify("TASK_READY", self.task_records[tid])
            await ready_queue.put(tid)

        active_workers: set[asyncio.Task] = set()
        completed_or_terminal = 0
        total_tasks = len(closure)

        # Build loop: runs until all tasks in closure reach a terminal state
        while completed_or_terminal < total_tasks:
            # Check if there are ready tasks and available worker capacity
            while not ready_queue.empty() and len(active_workers) < self.max_workers:
                tid = await ready_queue.get()
                record = self.task_records[tid]

                # Check if task was blocked while waiting in ready queue
                if record.status == TaskStatus.BLOCKED:
                    completed_or_terminal += 1
                    continue

                task = self.graph.nodes[tid]

                # Evaluate E2 rebuild decision
                # Gather upstream decision context
                upstream_decisions = {}
                upstream_fps = {}
                for prereq_id in task.rebuild_dependencies:
                    prereq_rec = self.task_records.get(prereq_id)
                    if prereq_rec and prereq_rec.status == TaskStatus.COMPLETED:
                        trace = self.rebuilder.cache.get_trace(prereq_id)
                        if trace:
                            upstream_fps[prereq_id] = trace.full_fingerprint

                decision = self.rebuilder.evaluate_task(
                    task,
                    self.graph,
                    upstream_decisions=upstream_decisions,
                    upstream_fingerprints=upstream_fps,
                )

                record.decision_action = decision.action.value
                record.decision_reason = decision.reason.value

                # ── CASE A: Task is UP_TO_DATE (SKIP) ──
                if decision.should_skip:
                    record.status = TaskStatus.SKIPPED
                    record.start_time = time.time()
                    record.end_time = record.start_time
                    record.duration = 0.0
                    self._notify("TASK_SKIPPED", record)
                    completed_or_terminal += 1

                    # Unblock downstream dependents immediately
                    for dep_id in sorted(task.dependents & closure):
                        self.in_degree[dep_id] -= 1
                        if self.in_degree[dep_id] == 0 and self.task_records[dep_id].status == TaskStatus.PENDING:
                            self.task_records[dep_id].status = TaskStatus.READY
                            self._notify("TASK_READY", self.task_records[dep_id])
                            await ready_queue.put(dep_id)
                    continue

                # ── CASE B: Task needs execution (BUILD) ──
                # Task remains READY until worker slot / semaphore is acquired
                record.status = TaskStatus.READY

                # Launch async execution worker which acquires semaphore and notifies TASK_STARTED
                worker_future = asyncio.create_task(
                    self._execute_worker(task, record)
                )
                active_workers.add(worker_future)

            # If no active workers and ready queue is empty, break (remaining tasks are blocked or done)
            if not active_workers:
                break

            # Wait for at least one worker to finish
            done, active_workers = await asyncio.wait(
                active_workers, return_when=asyncio.FIRST_COMPLETED
            )

            for finished_task in done:
                res: ExecutionResult = await finished_task
                tid = res.task_id
                record = self.task_records[tid]
                task = self.graph.nodes[tid]

                async with self._lock:
                    if record.status == TaskStatus.RUNNING:
                        self._current_running = max(0, self._current_running - 1)

                completed_or_terminal += 1
                record.end_time = res.end_time
                record.duration = res.duration
                record.worker_id = res.worker_id
                record.return_code = res.return_code
                record.stdout = res.stdout
                record.stderr = res.stderr

                # Handle Task Success
                if res.success:
                    # Persist trace in E2 cache ONLY after verified output validation
                    try:
                        self.rebuilder.record_success(task, self.graph)
                        record.status = TaskStatus.COMPLETED
                        self._notify("TASK_COMPLETED", record)

                        # Unblock downstream dependents
                        for dep_id in sorted(task.dependents & closure):
                            self.in_degree[dep_id] -= 1
                            if self.in_degree[dep_id] == 0 and self.task_records[dep_id].status == TaskStatus.PENDING:
                                self.task_records[dep_id].status = TaskStatus.READY
                                self._notify("TASK_READY", self.task_records[dep_id])
                                await ready_queue.put(dep_id)
                    except Exception as e:
                        # Output validation failed or cache error
                        record.status = TaskStatus.FAILED
                        record.error_message = f"Trace recording failed: {e}"
                        self._notify("TASK_FAILED", record)
                        self._block_downstream(tid, closure)
                else:
                    # Task execution failed
                    record.status = TaskStatus.FAILED
                    record.error_message = res.error_message or res.stderr
                    self._notify("TASK_FAILED", record)
                    # Block all downstream tasks
                    self._block_downstream(tid, closure)

        # Mark any remaining PENDING or READY tasks as BLOCKED
        for tid in closure:
            rec = self.task_records[tid]
            if rec.status in (TaskStatus.PENDING, TaskStatus.READY):
                rec.status = TaskStatus.BLOCKED
                rec.error_message = "Prerequisite failed or graph deadlocked."

        build_end = time.time()
        duration = build_end - build_start

        completed_count = sum(1 for r in self.task_records.values() if r.status == TaskStatus.COMPLETED)
        skipped_count = sum(1 for r in self.task_records.values() if r.status == TaskStatus.SKIPPED)
        failed_count = sum(1 for r in self.task_records.values() if r.status == TaskStatus.FAILED)
        blocked_count = sum(1 for r in self.task_records.values() if r.status == TaskStatus.BLOCKED)

        overall_success = failed_count == 0 and blocked_count == 0

        return BuildResult(
            target=target_name,
            success=overall_success,
            total_tasks=len(closure),
            completed_count=completed_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            blocked_count=blocked_count,
            start_time=build_start,
            end_time=build_end,
            duration=duration,
            max_workers=self.max_workers,
            peak_concurrency=self._peak_concurrency,
            task_records=self.task_records,
        )

    async def _execute_worker(
        self, task: TaskNode, record: TaskRecord
    ) -> ExecutionResult:
        """Run a single task through the process executor."""
        async def on_worker_acquired(wid: int) -> None:
            record.status = TaskStatus.RUNNING
            record.worker_id = wid
            record.start_time = time.time()
            async with self._lock:
                self._current_running += 1
                if self._current_running > self._peak_concurrency:
                    self._peak_concurrency = self._current_running
            self._notify("TASK_STARTED", record)

        return await self.executor.execute_task(
            task, on_start=on_worker_acquired
        )
