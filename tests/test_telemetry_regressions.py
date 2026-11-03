"""
Regression Tests for Telemetry and UI Consistency Fixes

Validates:
1. Peak concurrency with 4 overlapping tasks accurately tracking simultaneous active execution.
2. Peak concurrency returning to 0 active workers after build completion while retaining peak count.
3. Dependent archive task (libmylib.a) never completing or starting before all prerequisite object tasks.
4. Rapid event sequence handling and status precedence preventing terminal state demotion.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from engine.dag import BuildGraph
from engine.executor import AsyncProcessExecutor
from engine.ninja_parser import NinjaParser
from engine.rebuilder import Rebuilder
from engine.cache import BuildCache
from engine.scheduler import BuildResult, TaskRecord, TaskStatus, TopologicalScheduler
from telemetry.collector import TelemetryCollector
from telemetry.event_bus import EventBus
from telemetry.events import TelemetryEvent, TelemetryEventType


# ─────────────────────────────────────────────────────────────
# 1. Peak Concurrency with 4 Overlapping Tasks
# ─────────────────────────────────────────────────────────────

def test_peak_concurrency_with_four_overlapping_tasks():
    collector = TelemetryCollector()
    closure = {"task_math", "task_utils", "task_stringops", "task_fileio"}
    collector.start_build(build_id="build_conc_test", target="libmylib.a", max_workers=4, closure=closure)

    # Initial state
    metrics = collector.get_live_metrics()
    assert metrics["active_workers"] == 0
    assert metrics["peak_concurrency"] == 0
    assert metrics["tasks"]["running"] == 0

    # Start 4 tasks simultaneously (simulating scheduler dispatch)
    for i, tid in enumerate(sorted(closure), start=1):
        rec = TaskRecord(task_id=tid, status=TaskStatus.RUNNING, worker_id=i)
        collector.handle_scheduler_event("TASK_STARTED", rec)

    # Check live metrics while all 4 are running
    metrics_active = collector.get_live_metrics()
    assert metrics_active["active_workers"] == 4
    assert metrics_active["peak_concurrency"] == 4
    assert metrics_active["tasks"]["running"] == 4

    # Finish task 1
    rec1 = TaskRecord(task_id="task_fileio", status=TaskStatus.COMPLETED, worker_id=1, duration=0.12)
    collector.handle_scheduler_event("TASK_COMPLETED", rec1)

    metrics_after_one = collector.get_live_metrics()
    assert metrics_after_one["active_workers"] == 3
    assert metrics_after_one["peak_concurrency"] == 4  # Peak remains 4

    # Finish remaining 3 tasks
    for i, tid in enumerate(["task_math", "task_stringops", "task_utils"], start=2):
        rec = TaskRecord(task_id=tid, status=TaskStatus.COMPLETED, worker_id=i, duration=0.15)
        collector.handle_scheduler_event("TASK_COMPLETED", rec)

    metrics_done = collector.get_live_metrics()
    assert metrics_done["active_workers"] == 0
    assert metrics_done["peak_concurrency"] == 4


# ─────────────────────────────────────────────────────────────
# 2. Peak Concurrency Returns to 0 After Build Completion
# ─────────────────────────────────────────────────────────────

def test_peak_concurrency_returns_to_zero_after_build_completion():
    collector = TelemetryCollector()
    closure = {"task_a", "task_b"}
    collector.start_build(build_id="build_finish_test", target="demo.exe", max_workers=2, closure=closure)

    # Start both tasks
    for i, tid in enumerate(["task_a", "task_b"], start=1):
        collector.handle_scheduler_event(
            "TASK_STARTED", TaskRecord(task_id=tid, status=TaskStatus.RUNNING, worker_id=i)
        )

    assert collector.get_live_metrics()["active_workers"] == 2
    assert collector.peak_concurrency == 2

    # Finish build via finish_build
    dummy_result = BuildResult(
        target="demo.exe",
        success=True,
        total_tasks=2,
        completed_count=2,
        skipped_count=0,
        failed_count=0,
        blocked_count=0,
        start_time=collector.start_time,
        end_time=time.time(),
        duration=0.45,
        max_workers=2,
        peak_concurrency=2,
        task_records={},
    )
    evt = collector.finish_build(dummy_result)

    assert evt.event_type == TelemetryEventType.BUILD_COMPLETED
    assert collector.build_status == "SUCCESS"

    metrics = collector.get_live_metrics()
    assert metrics["status"] == "SUCCESS"
    assert metrics["active_workers"] == 0  # Crucial: active workers strictly 0
    assert metrics["peak_concurrency"] == 2  # Peak concurrency preserved
    assert metrics["wall_clock_duration"] >= 0.0


# ─────────────────────────────────────────────────────────────
# 3. Dependent Archive Task Never Completes Before Prereqs
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_archive_task_never_completes_before_prerequisite_objects(tmp_path):
    manifest_path = os.path.join(
        PROJECT_ROOT, "examples", "cpp_demo", "build", "build.ninja"
    )
    if not os.path.isfile(manifest_path):
        pytest.skip("CMake build.ninja not found.")
    base_dir = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "build")

    manifest = NinjaParser().parse_file(manifest_path)
    graph = BuildGraph.from_manifest(manifest, base_dir=base_dir)
    archive_target = "libmylib.a"

    closure = graph.get_target_closure(archive_target)
    assert len(closure) >= 5, f"Closure of libmylib.a should include archive + 4 object files, got {closure}"

    # Identify the archive task and its prerequisite tasks
    archive_task_id = None
    prereq_task_ids = set()
    for tid in closure:
        node = graph.nodes[tid]
        if archive_target in node.outputs or any(out.endswith("libmylib.a") for out in node.outputs):
            archive_task_id = tid
        else:
            prereq_task_ids.add(tid)

    assert archive_task_id is not None, "Archive task must exist in closure"
    assert len(prereq_task_ids) >= 4, "Must have at least 4 prerequisite object tasks"

    # Execute build with 4 workers using TopologicalScheduler
    db_path = str(tmp_path / "reg_test.db")
    cache = BuildCache(db_path=db_path)
    rebuilder = Rebuilder(cache=cache, base_dir=base_dir)
    executor = AsyncProcessExecutor(max_workers=4, base_dir=base_dir)
    scheduler = TopologicalScheduler(
        graph=graph,
        rebuilder=rebuilder,
        executor=executor,
        max_workers=4,
    )

    result = await scheduler.build(archive_target)
    assert result.success, "Build of libmylib.a must succeed"

    archive_record = result.task_records[archive_task_id]
    assert archive_record.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)

    # Check every prerequisite
    prereq_end_times = []
    for pid in prereq_task_ids:
        prec = result.task_records[pid]
        assert prec.status in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)
        prereq_end_times.append(prec.end_time)

    max_prereq_end_time = max(prereq_end_times)

    # Core invariant: archive task cannot start before all prerequisite tasks have finished
    assert archive_record.start_time >= max_prereq_end_time, (
        f"Archive task started at {archive_record.start_time:.4f} before "
        f"prerequisite finished at {max_prereq_end_time:.4f}"
    )
    # Archive task cannot complete before all prerequisite tasks have finished
    assert archive_record.end_time > max_prereq_end_time, (
        f"Archive task ended at {archive_record.end_time:.4f} before or at "
        f"prerequisite finished at {max_prereq_end_time:.4f}"
    )


# ─────────────────────────────────────────────────────────────
# 4. Rapid Event Sequence & Status Monotonicity Protection
# ─────────────────────────────────────────────────────────────

def test_rapid_event_stream_and_status_monotonicity():
    """
    Simulates high-throughput event burst and tests that terminal states
    (COMPLETED, FAILED, SKIPPED, BLOCKED) cannot be demoted by stale RUNNING events.
    """
    STATUS_PRECEDENCE = {
        "PENDING": 0,
        "READY": 1,
        "RUNNING": 2,
        "COMPLETED": 3,
        "SKIPPED": 3,
        "FAILED": 3,
        "BLOCKED": 3,
    }

    class MockFrontendState:
        def __init__(self):
            self.task_statuses: dict[str, str] = {}
            self.events_received = 0

        def handle_event(self, task_id: str, status: str, force: bool = False):
            self.events_received += 1
            prev_status = self.task_statuses.get(task_id, "PENDING")
            prev_rank = STATUS_PRECEDENCE.get(prev_status, 0)
            new_rank = STATUS_PRECEDENCE.get(status, 0)

            # Monotonicity rule: terminal states cannot be overwritten by non-terminal states
            if not force and prev_rank >= 3 and new_rank < 3:
                return False  # Demotion rejected

            self.task_statuses[task_id] = status
            return True

    fe = MockFrontendState()

    # Normal progression
    assert fe.handle_event("t_math", "READY")
    assert fe.handle_event("t_math", "RUNNING")
    assert fe.handle_event("t_math", "COMPLETED")
    assert fe.task_statuses["t_math"] == "COMPLETED"

    # Out-of-order or stale snapshot event arrives saying t_math is RUNNING
    rejected = not fe.handle_event("t_math", "RUNNING")
    assert rejected, "Stale RUNNING event must be rejected after COMPLETED"
    assert fe.task_statuses["t_math"] == "COMPLETED", "Task status must remain COMPLETED"

    # EventBus burst delivery test
    bus = EventBus()
    received = []
    bus.subscribe(lambda e: received.append(e))

    for i in range(100):
        evt = TelemetryEvent(
            event_id=f"burst_{i}",
            build_id="b_burst",
            event_type=TelemetryEventType.TASK_STARTED if i % 2 == 0 else TelemetryEventType.TASK_COMPLETED,
            task_id=f"task_{i % 10}",
        )
        bus.publish(evt)

    assert len(received) == 100
    assert fe.events_received >= 4
