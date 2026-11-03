"""
Telemetry Collector & Aggregator

Maintains real-time metrics, active task states, peak concurrency, and CPU times.
Consumes lifecycle events from the scheduler and translates them into structured
TelemetryEvent records distributed via EventBus and persisted via TelemetryDB.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from engine.dag import BuildGraph
from engine.scheduler import BuildResult, TaskRecord, TaskStatus
from telemetry.db import TelemetryDB
from telemetry.event_bus import EventBus
from telemetry.events import TelemetryEvent, TelemetryEventType

logger = logging.getLogger(__name__)


class TelemetryCollector:
    """
    Collects live telemetry, aggregates metrics, and coordinates persistence & broadcasting.
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        db: Optional[TelemetryDB] = None,
    ):
        self.event_bus = event_bus or EventBus()
        self.db = db
        self._event_counter = 0

        # Current build state
        self.build_id: Optional[str] = None
        self.target: str = "all"
        self.build_status: str = "IDLE"  # IDLE, RUNNING, SUCCESS, FAILED
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self.max_workers: int = 4
        self.peak_concurrency: int = 0
        self.active_workers: set[int] = set()
        self._active_task_ids: set[str] = set()

        # Task tracking
        self.task_states: dict[str, str] = {}
        self.task_durations: dict[str, float] = {}
        self.task_workers: dict[str, int] = {}
        self.task_details: dict[str, dict[str, Any]] = {}
        self.recent_events: list[dict[str, Any]] = []

        # Reference to current graph for metadata lookups
        self.current_graph: Optional[BuildGraph] = None

    def start_build(
        self,
        build_id: str,
        target: str,
        max_workers: int,
        graph: Optional[BuildGraph] = None,
        closure: Optional[set[str]] = None,
    ) -> TelemetryEvent:
        """Initialize telemetry for a new build session and emit BUILD_STARTED."""
        self.build_id = build_id
        self.target = target
        self.max_workers = max_workers
        self.build_status = "RUNNING"
        self.start_time = time.time()
        self.end_time = 0.0
        self.peak_concurrency = 0
        self.active_workers.clear()
        self._active_task_ids.clear()
        self._event_counter = 0
        self.recent_events.clear()
        self.current_graph = graph

        # Initialize all tasks in closure to PENDING
        self.task_states.clear()
        self.task_durations.clear()
        self.task_workers.clear()
        self.task_details.clear()

        if closure:
            for tid in sorted(closure):
                self.task_states[tid] = TaskStatus.PENDING.value
                self.task_details[tid] = {
                    "task_id": tid,
                    "status": TaskStatus.PENDING.value,
                    "duration": 0.0,
                    "worker_id": 0,
                }

        evt = self._create_event(
            event_type=TelemetryEventType.BUILD_STARTED,
            status="RUNNING",
            message=f"Build started for target '{target}' with {max_workers} worker(s).",
            metadata={
                "target": target,
                "max_workers": max_workers,
                "total_tasks": len(closure) if closure else 0,
            },
        )
        self._dispatch(evt)
        return evt

    def handle_scheduler_event(
        self, event_type_str: str, record: TaskRecord
    ) -> Optional[TelemetryEvent]:
        """
        Callback bridge hooked into TopologicalScheduler.on_event.
        Maps scheduler notifications into typed TelemetryEvents.
        """
        if not self.build_id:
            return None

        tid = record.task_id
        task_node = self.current_graph.nodes.get(tid) if self.current_graph else None
        task_name = task_node.outputs[0] if task_node and task_node.outputs else tid

        try:
            evt_type = TelemetryEventType(event_type_str)
        except ValueError:
            return None

        # Update local state tracking
        self.task_states[tid] = record.status.value
        self.task_durations[tid] = record.duration
        if record.worker_id:
            self.task_workers[tid] = record.worker_id

        # Concurrency tracking (simultaneous active tasks)
        if evt_type == TelemetryEventType.TASK_STARTED:
            self._active_task_ids.add(tid)
            if record.worker_id:
                self.active_workers.add(record.worker_id)
            current_active = len(self._active_task_ids)
            if current_active > self.peak_concurrency:
                self.peak_concurrency = current_active
        elif evt_type in (
            TelemetryEventType.TASK_COMPLETED,
            TelemetryEventType.TASK_FAILED,
        ):
            self._active_task_ids.discard(tid)
            if record.worker_id:
                self.active_workers.discard(record.worker_id)

        # Message formulation
        msg = record.error_message or record.decision_reason or ""
        if evt_type == TelemetryEventType.TASK_SKIPPED:
            msg = f"Skipped ({record.decision_reason or 'UP_TO_DATE'})"
        elif evt_type == TelemetryEventType.TASK_COMPLETED:
            msg = f"Completed in {record.duration:.3f}s"
        elif evt_type == TelemetryEventType.TASK_FAILED:
            msg = f"Failed (exit code {record.return_code})"

        self.task_details[tid] = {
            "task_id": tid,
            "status": record.status.value,
            "duration": record.duration,
            "worker_id": record.worker_id,
            "return_code": record.return_code,
            "error_message": record.error_message,
            "stdout": record.stdout,
            "stderr": record.stderr,
        }

        evt = self._create_event(
            event_type=evt_type,
            task_id=tid,
            task_name=task_name,
            worker_id=record.worker_id or None,
            status=record.status.value,
            duration=record.duration if record.duration > 0 else None,
            message=msg,
            stdout=record.stdout[-1000:] if record.stdout else None,
            stderr=record.stderr[-1000:] if record.stderr else None,
        )
        self._dispatch(evt)
        return evt

    def finish_build(self, result: BuildResult) -> TelemetryEvent:
        """Finalize telemetry after scheduler run and record summary."""
        self.end_time = result.end_time
        self.build_status = "SUCCESS" if result.success else "FAILED"
        self.peak_concurrency = max(self.peak_concurrency, result.peak_concurrency)
        self.active_workers.clear()
        self._active_task_ids.clear()

        evt_type = (
            TelemetryEventType.BUILD_COMPLETED
            if result.success
            else TelemetryEventType.BUILD_FAILED
        )

        total_cpu = sum(self.task_durations.values())
        avg_duration = (
            total_cpu / (result.completed_count + result.failed_count)
            if (result.completed_count + result.failed_count) > 0
            else 0.0
        )

        evt = self._create_event(
            event_type=evt_type,
            status=self.build_status,
            duration=result.duration,
            message=f"Build {self.build_status} in {result.duration:.3f}s. "
            f"({result.completed_count} completed, {result.skipped_count} skipped, "
            f"{result.failed_count} failed, {result.blocked_count} blocked)",
            metadata={
                "target": result.target,
                "duration": result.duration,
                "cpu_time": total_cpu,
                "average_task_duration": avg_duration,
                "peak_concurrency": max(self.peak_concurrency, result.peak_concurrency),
                "total_tasks": result.total_tasks,
                "completed": result.completed_count,
                "skipped": result.skipped_count,
                "failed": result.failed_count,
                "blocked": result.blocked_count,
            },
        )
        self._dispatch(evt)

        # Persist summary in TelemetryDB if available
        if self.db:
            build_summary = {
                "build_id": self.build_id,
                "target": result.target,
                "status": self.build_status,
                "start_time": result.start_time,
                "end_time": result.end_time,
                "duration": result.duration,
                "max_workers": result.max_workers,
                "peak_concurrency": max(self.peak_concurrency, result.peak_concurrency),
                "total_tasks": result.total_tasks,
                "completed_count": result.completed_count,
                "skipped_count": result.skipped_count,
                "failed_count": result.failed_count,
                "blocked_count": result.blocked_count,
                "cpu_time": total_cpu,
            }
            try:
                self.db.record_build(build_summary)
            except Exception as e:
                logger.error(f"Failed to persist build summary: {e}", exc_info=True)

        return evt

    def get_live_metrics(self) -> dict[str, Any]:
        """Aggregate and return snapshot of current build metrics."""
        now = time.time()
        wall_time = (
            (self.end_time or now) - self.start_time
            if self.start_time > 0
            else 0.0
        )

        total_tasks = len(self.task_states)
        completed = sum(1 for s in self.task_states.values() if s == TaskStatus.COMPLETED.value)
        skipped = sum(1 for s in self.task_states.values() if s == TaskStatus.SKIPPED.value)
        failed = sum(1 for s in self.task_states.values() if s == TaskStatus.FAILED.value)
        blocked = sum(1 for s in self.task_states.values() if s == TaskStatus.BLOCKED.value)
        running = sum(1 for s in self.task_states.values() if s == TaskStatus.RUNNING.value)
        pending = sum(1 for s in self.task_states.values() if s in (TaskStatus.PENDING.value, TaskStatus.READY.value))

        total_cpu = sum(self.task_durations.values())
        executed_count = completed + failed
        avg_duration = (total_cpu / executed_count) if executed_count > 0 else 0.0

        active_count = len(self._active_task_ids) if self.build_status == "RUNNING" else 0
        return {
            "build_id": self.build_id,
            "target": self.target,
            "status": self.build_status,
            "wall_clock_duration": round(wall_time, 3),
            "total_cpu_time": round(total_cpu, 3),
            "average_task_duration": round(avg_duration, 3),
            "max_workers": self.max_workers,
            "active_workers": active_count,
            "peak_concurrency": self.peak_concurrency,
            "tasks": {
                "total": total_tasks,
                "completed": completed,
                "skipped": skipped,
                "running": running,
                "pending": pending,
                "failed": failed,
                "blocked": blocked,
            },
        }

    def _create_event(self, **kwargs) -> TelemetryEvent:
        self._event_counter += 1
        evt_id = f"{self.build_id or 'sys'}_evt_{self._event_counter:04d}"
        return TelemetryEvent(event_id=evt_id, build_id=self.build_id or "sys", **kwargs)

    def _dispatch(self, event: TelemetryEvent) -> None:
        """Send event to EventBus and persist in SQLite."""
        self.recent_events.append(event.to_dict())
        if len(self.recent_events) > 500:
            self.recent_events.pop(0)

        # Distribute via EventBus
        self.event_bus.publish(event)

        # Persist to DB if active
        if self.db:
            try:
                self.db.record_event(event)
            except Exception as e:
                logger.error(f"Failed to persist event {event.event_id}: {e}", exc_info=True)
