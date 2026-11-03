"""
Tests for Phase 5: E4 — Build Telemetry Engine & API

Validates:
 1. TelemetryEvent creation, ISO-8601 formatting, and JSON serialization.
 2. EventBus synchronous publish/subscribe and error isolation.
 3. EventBus async queues and congestion handling.
 4. TelemetryDB SQLite schema, builds, events, and comparison persistence.
 5. TelemetryCollector build lifecycle transitions and concurrency tracking.
 6. TelemetryCollector metric aggregations (CPU time, average task time, counters).
 7. Benchmark performance comparison formulas (speedup, efficiency, time saved).
 8. FastAPI REST endpoints (/api/status, /api/graph, /api/builds, /api/comparison).
 9. FastAPI build session launch & conflict handling.
10. WebSocket /ws/telemetry live streaming and INIT handshake.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import pytest
from starlette.testclient import TestClient

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from api.main import app, clean_artifacts
from engine.scheduler import BuildResult, TaskRecord, TaskStatus
from telemetry.collector import TelemetryCollector
from telemetry.db import TelemetryDB
from telemetry.event_bus import EventBus
from telemetry.events import TelemetryEvent, TelemetryEventType
from telemetry.report import BenchmarkComparison, calculate_comparison


# ─────────────────────────────────────────────────────────────
# 1. Telemetry Event Model
# ─────────────────────────────────────────────────────────────

def test_telemetry_event_model():
    evt = TelemetryEvent(
        event_id="evt_001",
        build_id="build_test_123",
        event_type=TelemetryEventType.BUILD_STARTED,
        status="RUNNING",
        message="Starting test build",
        metadata={"target": "all", "max_workers": 4},
    )

    assert evt.event_id == "evt_001"
    assert evt.build_id == "build_test_123"
    assert evt.event_type == TelemetryEventType.BUILD_STARTED
    assert "T" in evt.iso_time  # ISO-8601 formatted
    assert evt.metadata["target"] == "all"

    d = evt.to_dict()
    assert d["event_type"] == "BUILD_STARTED"
    assert d["status"] == "RUNNING"
    # Ensure json serializable
    json_str = json.dumps(d)
    assert "evt_001" in json_str


# ─────────────────────────────────────────────────────────────
# 2. EventBus Synchronous Pub/Sub & Error Isolation
# ─────────────────────────────────────────────────────────────

def test_event_bus_sync_pubsub():
    bus = EventBus()
    received_1 = []
    received_2 = []

    def sub1(e):
        received_1.append(e)

    def sub2(e):
        received_2.append(e)

    def faulty_sub(e):
        raise RuntimeError("Subscriber explosion")

    bus.subscribe(sub1)
    bus.subscribe(faulty_sub)
    bus.subscribe(sub2)

    evt = TelemetryEvent(
        event_id="e1",
        build_id="b1",
        event_type=TelemetryEventType.TASK_STARTED,
        task_id="task_compile",
    )
    bus.publish(evt)

    # Faulty subscriber must not disrupt sub1 and sub2
    assert len(received_1) == 1
    assert len(received_2) == 1
    assert received_1[0].task_id == "task_compile"
    assert received_2[0].task_id == "task_compile"

    # Test unsubscribe
    bus.unsubscribe(sub1)
    bus.publish(evt)
    assert len(received_1) == 1  # Unchanged
    assert len(received_2) == 2


# ─────────────────────────────────────────────────────────────
# 3. EventBus Async Queues
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_bus_async_queues():
    bus = EventBus()
    q = bus.register_async_queue(maxsize=2)

    evt1 = TelemetryEvent(event_id="e1", build_id="b", event_type=TelemetryEventType.TASK_READY)
    evt2 = TelemetryEvent(event_id="e2", build_id="b", event_type=TelemetryEventType.TASK_STARTED)
    evt3 = TelemetryEvent(event_id="e3", build_id="b", event_type=TelemetryEventType.TASK_COMPLETED)

    bus.publish(evt1)
    bus.publish(evt2)
    # Exceed maxsize: oldest should be dropped
    bus.publish(evt3)

    assert q.qsize() == 2
    r1 = await q.get()
    r2 = await q.get()
    assert r1.event_id == "e2"
    assert r2.event_id == "e3"

    bus.unregister_async_queue(q)


# ─────────────────────────────────────────────────────────────
# 4. TelemetryDB SQLite Persistence
# ─────────────────────────────────────────────────────────────

def test_telemetry_db_crud(tmp_path):
    db_file = str(tmp_path / "telemetry_test.db")
    db = TelemetryDB(db_file)

    # Test build persistence
    build_data = {
        "build_id": "b_100",
        "target": "demo.exe",
        "status": "SUCCESS",
        "start_time": 1000.0,
        "end_time": 1002.5,
        "duration": 2.5,
        "max_workers": 4,
        "peak_concurrency": 4,
        "total_tasks": 5,
        "completed_count": 5,
        "skipped_count": 0,
        "failed_count": 0,
        "blocked_count": 0,
        "cpu_time": 7.2,
    }
    db.record_build(build_data)

    retrieved = db.get_build("b_100")
    assert retrieved is not None
    assert retrieved["target"] == "demo.exe"
    assert retrieved["duration"] == 2.5
    assert retrieved["completed_count"] == 5

    # Test event persistence
    evt = TelemetryEvent(
        event_id="evt_01",
        build_id="b_100",
        event_type=TelemetryEventType.TASK_COMPLETED,
        task_id="T1",
        task_name="main.cpp.obj",
        worker_id=2,
        status="COMPLETED",
        duration=0.45,
        message="Compiled successfully",
        stdout="GCC output",
    )
    db.record_event(evt)

    events = db.get_build_events("b_100")
    assert len(events) == 1
    assert events[0]["event_id"] == "evt_01"
    assert events[0]["task_name"] == "main.cpp.obj"
    assert events[0]["worker_id"] == 2
    assert events[0]["duration"] == 0.45

    # Test comparison persistence
    comp_data = {
        "comparison_id": "cmp_01",
        "target": "all",
        "sequential_duration": 5.0,
        "parallel_duration": 1.9,
        "worker_count": 4,
        "speedup": 2.63,
        "efficiency": 65.8,
        "time_saved": 3.1,
        "pct_reduction": 62.0,
        "timestamp": time.time(),
    }
    db.record_comparison(comp_data)

    latest = db.get_latest_comparison()
    assert latest is not None
    assert latest["comparison_id"] == "cmp_01"
    assert latest["speedup"] == 2.63
    assert latest["efficiency"] == 65.8

    all_comps = db.get_comparisons(limit=10)
    assert len(all_comps) == 1

    db.close()


# ─────────────────────────────────────────────────────────────
# 5. TelemetryCollector Lifecycle & Concurrency Tracking
# ─────────────────────────────────────────────────────────────

def test_telemetry_collector_lifecycle():
    bus = EventBus()
    db = TelemetryDB(":memory:")
    collector = TelemetryCollector(event_bus=bus, db=db)

    closure = {"T1", "T2", "T3"}
    collector.start_build(
        build_id="b_live",
        target="all",
        max_workers=4,
        closure=closure,
    )

    assert collector.build_status == "RUNNING"
    assert collector.max_workers == 4
    assert len(collector.task_states) == 3

    # Task 1 starts on Worker 1
    rec1 = TaskRecord(task_id="T1", status=TaskStatus.RUNNING, worker_id=1)
    collector.handle_scheduler_event("TASK_STARTED", rec1)
    assert collector.peak_concurrency == 1
    assert len(collector.active_workers) == 1

    # Task 2 starts on Worker 2
    rec2 = TaskRecord(task_id="T2", status=TaskStatus.RUNNING, worker_id=2)
    collector.handle_scheduler_event("TASK_STARTED", rec2)
    assert collector.peak_concurrency == 2
    assert len(collector.active_workers) == 2

    # Task 1 completes
    rec1.status = TaskStatus.COMPLETED
    rec1.duration = 0.5
    collector.handle_scheduler_event("TASK_COMPLETED", rec1)
    assert len(collector.active_workers) == 1

    # Task 3 is skipped
    rec3 = TaskRecord(task_id="T3", status=TaskStatus.SKIPPED, decision_reason="UP_TO_DATE")
    collector.handle_scheduler_event("TASK_SKIPPED", rec3)

    # Task 2 completes
    rec2.status = TaskStatus.COMPLETED
    rec2.duration = 0.8
    collector.handle_scheduler_event("TASK_COMPLETED", rec2)
    assert len(collector.active_workers) == 0

    # Finish build
    result = BuildResult(
        target="all",
        success=True,
        total_tasks=3,
        completed_count=2,
        skipped_count=1,
        failed_count=0,
        blocked_count=0,
        start_time=collector.start_time,
        end_time=time.time(),
        duration=1.2,
        max_workers=4,
        peak_concurrency=2,
        task_records={"T1": rec1, "T2": rec2, "T3": rec3},
    )
    final_evt = collector.finish_build(result)
    assert final_evt.event_type == TelemetryEventType.BUILD_COMPLETED
    assert collector.build_status == "SUCCESS"

    # Verify metrics snapshot
    metrics = collector.get_live_metrics()
    assert metrics["tasks"]["total"] == 3
    assert metrics["tasks"]["completed"] == 2
    assert metrics["tasks"]["skipped"] == 1
    assert metrics["peak_concurrency"] == 2
    assert metrics["total_cpu_time"] == round(0.5 + 0.8, 3)

    # Verify DB received the summary
    saved_build = db.get_build("b_live")
    assert saved_build is not None
    assert saved_build["status"] == "SUCCESS"
    assert saved_build["completed_count"] == 2

    db.close()


# ─────────────────────────────────────────────────────────────
# 6. Benchmark Performance Formulas
# ─────────────────────────────────────────────────────────────

def test_benchmark_calculation_formulas():
    # Standard 4-worker comparison: 5.0s sequential, 2.0s parallel
    comp = calculate_comparison(
        target="all",
        sequential_duration=5.0,
        parallel_duration=2.0,
        worker_count=4,
    )

    # Speedup: 5.0 / 2.0 = 2.5x
    assert comp.speedup == 2.5
    # Parallel Efficiency: (2.5 / 4) * 100% = 62.5%
    assert comp.efficiency == 62.5
    # Time Saved: 5.0 - 2.0 = 3.0s
    assert comp.time_saved == 3.0
    # Percentage Reduction: (3.0 / 5.0) * 100% = 60.0%
    assert comp.pct_reduction == 60.0

    # Verify summary string
    summ = comp.summary()
    assert "2.50x" in summ
    assert "62.5%" in summ
    assert "3.000 s" in summ

    # Edge cases: zero or negative durations safely clamped
    comp_zero = calculate_comparison("all", 0.0, 0.0, 1)
    assert comp_zero.speedup >= 0.0
    assert comp_zero.efficiency >= 0.0


# ─────────────────────────────────────────────────────────────
# 7. FastAPI REST Endpoints
# ─────────────────────────────────────────────────────────────

def test_api_rest_endpoints():
    client = TestClient(app)

    # 1. GET /api/status
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert "status" in data
    assert "tasks" in data
    assert "max_workers" in data

    # 2. GET /api/graph
    res_graph = client.get("/api/graph?target=all")
    assert res_graph.status_code == 200
    g_data = res_graph.json()
    assert "nodes" in g_data
    assert "edges" in g_data
    assert "available_targets" in g_data
    assert len(g_data["nodes"]) == 9  # 9 tasks in cpp_demo 'all' closure

    # 3. GET /api/builds
    res_builds = client.get("/api/builds")
    assert res_builds.status_code == 200
    assert isinstance(res_builds.json(), list)

    # 4. GET /api/comparison
    res_comp = client.get("/api/comparison")
    assert res_comp.status_code == 200
    assert "latest" in res_comp.json()
    assert "history" in res_comp.json()

    # 5. GET / dashboard static file
    res_dash = client.get("/")
    assert res_dash.status_code == 200
    assert "Task Orchestration Engine" in res_dash.text


# ─────────────────────────────────────────────────────────────
# 8. FastAPI Build Request Launch
# ─────────────────────────────────────────────────────────────

def test_api_build_start():
    client = TestClient(app)

    res = client.post(
        "/api/build/start",
        json={"target": "demo.exe", "max_workers": 2, "clean": False},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "QUEUED"
    assert data["target"] == "demo.exe"
    assert data["max_workers"] == 2


# ─────────────────────────────────────────────────────────────
# 9. WebSocket /ws/telemetry Handshake
# ─────────────────────────────────────────────────────────────

def test_websocket_telemetry_handshake():
    client = TestClient(app)

    with client.websocket_connect("/ws/telemetry") as ws:
        # Upon connect, server immediately delivers INIT status
        init_msg = ws.receive_json()
        assert init_msg["type"] == "INIT"
        assert "tasks" in init_msg["data"]

        # Client sends ping, gets pong
        ws.send_text("ping")
        resp = ws.receive_text()
        assert resp == "pong"


# ─────────────────────────────────────────────────────────────
# 10. Collector Failure and Blocked Propagation
# ─────────────────────────────────────────────────────────────

def test_telemetry_failure_and_blocked_handling():
    bus = EventBus()
    db = TelemetryDB(":memory:")
    collector = TelemetryCollector(event_bus=bus, db=db)

    closure = {"compile_fail", "link_blocked"}
    collector.start_build("b_fail", "all", 2, closure=closure)

    # Task starts then fails
    rec_fail = TaskRecord(task_id="compile_fail", status=TaskStatus.RUNNING, worker_id=1)
    collector.handle_scheduler_event("TASK_STARTED", rec_fail)
    assert len(collector.active_workers) == 1

    rec_fail.status = TaskStatus.FAILED
    rec_fail.return_code = 1
    rec_fail.error_message = "Syntax error in source"
    collector.handle_scheduler_event("TASK_FAILED", rec_fail)
    assert len(collector.active_workers) == 0

    # Downstream task blocked
    rec_blocked = TaskRecord(
        task_id="link_blocked",
        status=TaskStatus.BLOCKED,
        error_message="Blocked by prerequisite",
    )
    collector.handle_scheduler_event("TASK_BLOCKED", rec_blocked)

    # Finish build as failure
    res = BuildResult(
        target="all",
        success=False,
        total_tasks=2,
        completed_count=0,
        skipped_count=0,
        failed_count=1,
        blocked_count=1,
        start_time=collector.start_time,
        end_time=time.time(),
        duration=0.5,
        max_workers=2,
        peak_concurrency=1,
        task_records={"compile_fail": rec_fail, "link_blocked": rec_blocked},
    )
    final_evt = collector.finish_build(res)
    assert final_evt.event_type == TelemetryEventType.BUILD_FAILED
    assert collector.build_status == "FAILED"

    metrics = collector.get_live_metrics()
    assert metrics["tasks"]["failed"] == 1
    assert metrics["tasks"]["blocked"] == 1

    db.close()


# ─────────────────────────────────────────────────────────────
# 11. End-to-End Orchestrator + Telemetry Flow on Real Manifest
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_end_to_end_build_telemetry_flow(tmp_path):
    manifest_p = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "build", "build.ninja")
    base_d = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "build")
    db_p = str(tmp_path / "e2e_telemetry.db")

    bus = EventBus()
    db = TelemetryDB(db_p)
    collector = TelemetryCollector(event_bus=bus, db=db)

    events_received = []
    bus.subscribe(lambda e: events_received.append(e))

    from engine.orchestrator import Orchestrator
    orch = Orchestrator(
        manifest_path=manifest_p,
        db_path=db_p,
        base_dir=base_d,
        max_workers=4,
        on_event=collector.handle_scheduler_event,
    )

    b_id = "b_e2e_001"
    closure = orch.graph.get_target_closure("all")
    collector.start_build(b_id, "all", 4, graph=orch.graph, closure=closure)

    result = await orch.build(target="all")
    collector.finish_build(result)
    orch.close()

    assert result.success is True
    assert result.completed_count + result.skipped_count == 9

    # Verify event stream
    assert len(events_received) > 0
    event_types = [e.event_type for e in events_received]
    assert TelemetryEventType.BUILD_STARTED in event_types
    assert TelemetryEventType.BUILD_COMPLETED in event_types

    # Verify SQLite recorded events and build
    persisted_build = db.get_build(b_id)
    assert persisted_build is not None
    assert persisted_build["total_tasks"] == 9
    assert persisted_build["status"] == "SUCCESS"

    db_events = db.get_build_events(b_id)
    assert len(db_events) > 0

    db.close()
