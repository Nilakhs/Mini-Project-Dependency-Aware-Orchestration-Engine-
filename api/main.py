"""
FastAPI Telemetry & Build Control Server

Provides REST and WebSocket endpoints for the Dependency-Aware Task Orchestration Engine:
- GET  /api/status: Current build engine metrics, active workers, task counters
- GET  /api/graph: DAG nodes and dependency edges with live statuses
- GET  /api/builds: Historical build execution summaries
- GET  /api/builds/{build_id}: Detailed build events and task breakdown
- GET  /api/comparison: Benchmark comparison metrics (sequential vs parallel)
- POST /api/build/start: Initiate a real build session (target, workers, clean)
- POST /api/benchmark/run: Execute automated 1-worker vs 4-worker comparison
- WS   /ws/telemetry: Live WebSocket streaming of build events and state transitions
- GET  /: Web Dashboard
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import sys
import time
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from engine.cache import BuildCache
from engine.dag import BuildGraph
from engine.ninja_parser import NinjaParser
from engine.orchestrator import Orchestrator
from telemetry.collector import TelemetryCollector
from telemetry.db import TelemetryDB
from telemetry.event_bus import EventBus
from telemetry.events import TelemetryEvent, TelemetryEventType
from telemetry.report import BenchmarkComparison, calculate_comparison
from api.websocket import ConnectionManager

logger = logging.getLogger("build_api")
logging.basicConfig(level=logging.INFO)

# Default paths for the C++ demonstration project
DEFAULT_MANIFEST = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "build", "build.ninja")
DEFAULT_BASE_DIR = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "build")
DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", "build_state.db")
DASHBOARD_DIR = os.path.join(PROJECT_ROOT, "dashboard")

# Global telemetry components
event_bus = EventBus()
db = TelemetryDB(DEFAULT_DB)
collector = TelemetryCollector(event_bus=event_bus, db=db)
ws_manager = ConnectionManager(event_bus=event_bus)

# Execution concurrency lock
build_lock = asyncio.Lock()
current_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background workers on startup, stop cleanly on shutdown."""
    ws_manager.start_background_broadcaster()
    yield
    await ws_manager.stop_background_broadcaster()
    db.close()


app = FastAPI(
    title="Dependency-Aware Parallel Task Orchestration Engine",
    description="Real-time build telemetry and task scheduling dashboard API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def clean_artifacts(base_dir: str = DEFAULT_BASE_DIR, db_path: str = DEFAULT_DB) -> None:
    """Safely delete compiled object files and binaries to reset build state."""
    patterns = [
        "demo.exe",
        "test_runner.exe",
        "libmylib.a",
        os.path.join("CMakeFiles", "mylib.dir", "src", "math.cpp.obj"),
        os.path.join("CMakeFiles", "mylib.dir", "src", "math.cpp.obj.d"),
        os.path.join("CMakeFiles", "mylib.dir", "src", "utils.cpp.obj"),
        os.path.join("CMakeFiles", "mylib.dir", "src", "utils.cpp.obj.d"),
        os.path.join("CMakeFiles", "mylib.dir", "src", "stringops.cpp.obj"),
        os.path.join("CMakeFiles", "mylib.dir", "src", "stringops.cpp.obj.d"),
        os.path.join("CMakeFiles", "mylib.dir", "src", "fileio.cpp.obj"),
        os.path.join("CMakeFiles", "mylib.dir", "src", "fileio.cpp.obj.d"),
        os.path.join("CMakeFiles", "demo.dir", "src", "main.cpp.obj"),
        os.path.join("CMakeFiles", "demo.dir", "src", "main.cpp.obj.d"),
        os.path.join("CMakeFiles", "test_runner.dir", "src", "test_runner.cpp.obj"),
        os.path.join("CMakeFiles", "test_runner.dir", "src", "test_runner.cpp.obj.d"),
    ]
    for p in patterns:
        full_p = os.path.join(base_dir, p)
        if os.path.isfile(full_p):
            try:
                os.remove(full_p)
            except OSError:
                pass

    # Clear build traces from cache
    try:
        cache = BuildCache(db_path=db_path)
        cache.clear()
        cache.close()
    except Exception as e:
        logger.warning(f"Could not clear build cache during clean: {e}")


# ── REQUEST MODELS ───────────────────────────────────────────────

class BuildRequest(BaseModel):
    target: str = Field(default="all", description="Target output or alias to build")
    max_workers: int = Field(default=4, ge=1, le=16, description="Parallel worker limit")
    clean: bool = Field(default=False, description="Wipe existing outputs and trace cache before building")


class BenchmarkRequest(BaseModel):
    target: str = Field(default="all", description="Target output to benchmark")
    parallel_workers: int = Field(default=4, ge=2, le=16, description="Workers for parallel run")


# ── REST ENDPOINTS ───────────────────────────────────────────────

@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    """Return live execution metrics and task state counts."""
    metrics = collector.get_live_metrics()
    metrics["is_busy"] = build_lock.locked()
    return metrics


@app.get("/api/graph")
async def get_graph(target: Optional[str] = None) -> dict[str, Any]:
    """
    Return dependency graph representation (nodes & edges)
    annotated with live execution status and duration.
    """
    if not os.path.exists(DEFAULT_MANIFEST):
        raise HTTPException(status_code=404, detail="build.ninja manifest not found.")

    parser = NinjaParser()
    manifest = parser.parse_file(DEFAULT_MANIFEST)
    graph = BuildGraph.from_manifest(manifest, parser=parser, base_dir=DEFAULT_BASE_DIR)

    closure = graph.get_target_closure(target)

    nodes = []
    for tid in sorted(closure):
        task = graph.nodes[tid]
        label = task.outputs[0] if task.outputs else tid
        # Clean relative label for display
        display_label = label.replace(DEFAULT_BASE_DIR, "").lstrip("/\\")
        
        status = collector.task_states.get(tid, "PENDING")
        duration = collector.task_durations.get(tid, 0.0)
        worker_id = collector.task_workers.get(tid, 0)
        details = collector.task_details.get(tid, {})

        nodes.append({
            "id": tid,
            "label": display_label,
            "rule": task.rule_name,
            "command": task.command,
            "outputs": task.outputs,
            "inputs": task.all_inputs,
            "explicit_inputs": task.explicit_inputs,
            "implicit_inputs": task.implicit_inputs,
            "order_only_inputs": task.order_only_inputs,
            "status": status,
            "duration": duration,
            "worker_id": worker_id,
            "stdout": details.get("stdout", ""),
            "stderr": details.get("stderr", ""),
            "error_message": details.get("error_message", ""),
        })

    edges = []
    for tid in sorted(closure):
        task = graph.nodes[tid]
        for prereq_id, dep_type in task.dependencies.items():
            if prereq_id in closure:
                edges.append({
                    "from": prereq_id,
                    "to": tid,
                    "type": dep_type.value,
                })

    available_targets = sorted(list(graph.phony_targets.keys()) + [
        n.outputs[0].replace(DEFAULT_BASE_DIR, "").lstrip("/\\")
        for n in graph.nodes.values() if n.outputs
    ])

    return {
        "nodes": nodes,
        "edges": edges,
        "defaults": graph.defaults,
        "available_targets": sorted(list(set(available_targets))),
        "closure_count": len(closure),
    }


@app.get("/api/builds")
async def get_builds(limit: int = 50) -> list[dict[str, Any]]:
    """Return historical build runs."""
    return db.get_builds(limit=limit)


@app.get("/api/builds/{build_id}")
async def get_build_detail(build_id: str) -> dict[str, Any]:
    """Return build summary and event timeline for a specific build session."""
    summary = db.get_build(build_id)
    if not summary:
        raise HTTPException(status_code=404, detail=f"Build ID '{build_id}' not found.")
    events = db.get_build_events(build_id)
    return {"summary": summary, "events": events}


@app.get("/api/comparison")
async def get_comparison() -> dict[str, Any]:
    """Return the most recent benchmark comparison and recent records."""
    latest = db.get_latest_comparison()
    recent = db.get_comparisons(limit=10)
    return {"latest": latest, "history": recent}


async def _run_build_session(
    target: str,
    max_workers: int,
    clean: bool,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    """Background build executor."""
    async with build_lock:
        if clean:
            clean_artifacts(DEFAULT_BASE_DIR, db_path=db_path)

        build_id = f"build_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        orch = Orchestrator(
            manifest_path=DEFAULT_MANIFEST,
            db_path=db_path,
            base_dir=DEFAULT_BASE_DIR,
            max_workers=max_workers,
            on_event=collector.handle_scheduler_event,
        )

        closure = orch.graph.get_target_closure(target)
        collector.start_build(
            build_id=build_id,
            target=target,
            max_workers=max_workers,
            graph=orch.graph,
            closure=closure,
        )

        try:
            result = await orch.build(target=target)
            collector.finish_build(result)
            return {
                "build_id": build_id,
                "success": result.success,
                "duration": result.duration,
                "total_tasks": result.total_tasks,
                "completed": result.completed_count,
                "skipped": result.skipped_count,
                "failed": result.failed_count,
                "blocked": result.blocked_count,
            }
        finally:
            orch.close()


from datetime import datetime, timezone


@app.post("/api/build/start")
async def start_build(req: BuildRequest) -> dict[str, Any]:
    """Trigger an asynchronous build session."""
    if build_lock.locked():
        raise HTTPException(status_code=409, detail="A build or benchmark is already in progress.")

    # Run build in background task so endpoint responds immediately
    asyncio.create_task(
        _run_build_session(
            target=req.target,
            max_workers=req.max_workers,
            clean=req.clean,
        )
    )

    return {
        "status": "QUEUED",
        "target": req.target,
        "max_workers": req.max_workers,
        "clean": req.clean,
    }


@app.post("/api/benchmark/run")
async def run_benchmark(req: BenchmarkRequest) -> dict[str, Any]:
    """
    Run empirical sequential (1 worker) vs parallel (N workers) comparison.
    Both runs start from a clean state to ensure fair wall-clock measurements.
    """
    if build_lock.locked():
        raise HTTPException(status_code=409, detail="Another build or benchmark is currently executing.")

    async with build_lock:
        # 1. Clean Sequential Build (p=1)
        clean_artifacts(DEFAULT_BASE_DIR)
        orch_seq = Orchestrator(
            DEFAULT_MANIFEST,
            db_path=DEFAULT_DB,
            base_dir=DEFAULT_BASE_DIR,
            max_workers=1,
            on_event=collector.handle_scheduler_event,
        )
        b_id_seq = f"bench_seq_{uuid.uuid4().hex[:6]}"
        closure_seq = orch_seq.graph.get_target_closure(req.target)
        collector.start_build(b_id_seq, req.target, 1, graph=orch_seq.graph, closure=closure_seq)
        res_seq = await orch_seq.build(target=req.target)
        collector.finish_build(res_seq)
        orch_seq.close()

        # Brief settle time
        await asyncio.sleep(0.5)

        # 2. Clean Parallel Build (p=req.parallel_workers)
        clean_artifacts(DEFAULT_BASE_DIR)
        orch_par = Orchestrator(
            DEFAULT_MANIFEST,
            db_path=DEFAULT_DB,
            base_dir=DEFAULT_BASE_DIR,
            max_workers=req.parallel_workers,
            on_event=collector.handle_scheduler_event,
        )
        b_id_par = f"bench_par_{uuid.uuid4().hex[:6]}"
        closure_par = orch_par.graph.get_target_closure(req.target)
        collector.start_build(b_id_par, req.target, req.parallel_workers, graph=orch_par.graph, closure=closure_par)
        res_par = await orch_par.build(target=req.target)
        collector.finish_build(res_par)
        orch_par.close()

        # 3. Calculate empirical metrics
        comp = calculate_comparison(
            target=req.target,
            sequential_duration=res_seq.duration,
            parallel_duration=res_par.duration,
            worker_count=req.parallel_workers,
        )
        db.record_comparison(comp.to_dict())

        # 4. Broadcast benchmark result event
        comp_event = TelemetryEvent(
            event_id=f"comp_{uuid.uuid4().hex[:6]}",
            build_id=b_id_par,
            event_type=TelemetryEventType.BUILD_COMPLETED,
            status="BENCHMARK_COMPLETE",
            message=f"Speedup: {comp.speedup}x, Efficiency: {comp.efficiency}%, Saved: {comp.time_saved}s",
            metadata=comp.to_dict(),
        )
        event_bus.publish(comp_event)

        return comp.to_dict()


# ── WEBSOCKET ENDPOINT ───────────────────────────────────────────

@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    """Live streaming WebSocket for events and state updates."""
    await ws_manager.connect(websocket)
    try:
        # Send initial status snapshot immediately on connect
        initial_metrics = collector.get_live_metrics()
        await websocket.send_json({"type": "INIT", "data": initial_metrics})

        while True:
            # Client heartbeat / command loop
            msg_text = await websocket.receive_text()
            # Client can send 'ping' or requests
            if msg_text == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"WebSocket client error: {e}")
        ws_manager.disconnect(websocket)


# ── DASHBOARD STATIC FILES ───────────────────────────────────────

if os.path.isdir(DASHBOARD_DIR):
    app.mount("/", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
