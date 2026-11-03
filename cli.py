"""
Dependency-Aware Task Orchestration Engine — Unified CLI

Provides command-line commands for demonstration, builds, benchmarking, and testing:
- python cli.py dashboard             : Launch live web dashboard and WebSocket server
- python cli.py build                 : Run parallel build (default 4 workers, target 'all')
- python cli.py build --workers 1     : Run sequential build (1 worker)
- python cli.py build --clean         : Wipe artifacts and force full rebuild
- python cli.py build --target <t>    : Build specific target closure (e.g. demo.exe)
- python cli.py benchmark             : Run reproducible multi-trial benchmark suite
- python cli.py history               : View past build execution history from SQLite
- python cli.py test                  : Run full automated pytest test suite
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from api.main import clean_artifacts, DEFAULT_BASE_DIR, DEFAULT_MANIFEST, DEFAULT_DB
from engine.orchestrator import Orchestrator
from engine.scheduler import TaskRecord, TaskStatus
from telemetry.collector import TelemetryCollector
from telemetry.db import TelemetryDB
from telemetry.event_bus import EventBus
from telemetry.events import TelemetryEventType


def print_banner(title: str):
    print("=" * 68)
    print(f"  {title.upper()}")
    print("=" * 68)


async def run_build_command(target: str = "all", workers: int = 4, clean: bool = False):
    print_banner(f"Task Orchestration Engine - Build: {target}")
    print(f"  Manifest: {DEFAULT_MANIFEST}")
    print(f"  Base Dir: {DEFAULT_BASE_DIR}")
    print(f"  Workers:  {workers}")
    print(f"  Clean:    {clean}")
    print("-" * 68)

    if clean:
        print("  [CLEAN] Removing previous build outputs and trace cache...")
        clean_artifacts(DEFAULT_BASE_DIR, db_path=DEFAULT_DB)

    # Initialize Telemetry
    bus = EventBus()
    db = TelemetryDB(DEFAULT_DB)
    collector = TelemetryCollector(event_bus=bus, db=db)

    def on_event_console(event_type: str, record: TaskRecord):
        status_colors = {
            "TASK_COMPLETED": "\033[92mCOMPLETED\033[0m",
            "TASK_SKIPPED": "\033[90mSKIPPED\033[0m",
            "TASK_FAILED": "\033[91mFAILED\033[0m",
            "TASK_BLOCKED": "\033[93mBLOCKED\033[0m",
            "TASK_STARTED": "\033[94mRUNNING\033[0m",
            "TASK_READY": "\033[36mREADY\033[0m",
        }
        color_status = status_colors.get(event_type, event_type)
        dur = f"({record.duration:.3f}s)" if record.duration > 0 else ""
        worker = f"[w{record.worker_id}]" if record.worker_id else ""
        print(f"  {color_status:<19} {worker:<6} {record.task_id} {dur}")

    build_id = f"cli_{int(time.time())}"
    orch = Orchestrator(
        manifest_path=DEFAULT_MANIFEST,
        db_path=DEFAULT_DB,
        base_dir=DEFAULT_BASE_DIR,
        max_workers=workers,
        on_event=collector.handle_scheduler_event,
    )

    # Add console listener to event bus
    bus.subscribe(lambda e: print(f"  [{e.event_type.value:<15}] {e.task_name or e.message}") if e.event_type in (
        TelemetryEventType.BUILD_STARTED, TelemetryEventType.BUILD_COMPLETED, TelemetryEventType.BUILD_FAILED
    ) else None)

    closure = orch.graph.get_target_closure(target)
    collector.start_build(build_id, target, workers, graph=orch.graph, closure=closure)

    t0 = time.perf_counter()
    res = await orch.build(target=target)
    collector.finish_build(res)
    orch.close()
    db.close()

    print("-" * 68)
    print(res.summary())


def run_history_command(limit: int = 10):
    print_banner(f"Recent Build Execution History (SQLite: {DEFAULT_DB})")
    db = TelemetryDB(DEFAULT_DB)
    builds = db.get_builds(limit=limit)
    db.close()

    if not builds:
        print("  No historical builds found in database.")
        return

    header = f"  {'Build ID':<24} {'Target':<10} {'Status':<10} {'Workers':<8} {'Time':<10} {'Tasks (C/S/F/B)':<16}"
    print(header)
    print("  " + "-" * 78)
    for b in builds:
        b_id = b["build_id"][:22]
        tgt = b["target"][:10]
        st = b["status"]
        wk = f"{b['max_workers']}w (pk:{b['peak_concurrency']})"
        dur = f"{b['duration']:.3f}s"
        counts = f"{b['completed_count']}/{b['skipped_count']}/{b['failed_count']}/{b['blocked_count']}"
        print(f"  {b_id:<24} {tgt:<10} {st:<10} {wk:<8} {dur:<10} {counts:<16}")
    print()


def run_dashboard_command(port: int = 8000, host: str = "127.0.0.1"):
    import uvicorn
    print_banner("Task Orchestration Engine - Live Dashboard")
    print(f"  URL:  http://{host}:{port}")
    print(f"  Docs: http://{host}:{port}/docs")
    print("  Press Ctrl+C to stop.")
    print("-" * 68)
    uvicorn.run("api.main:app", host=host, port=port, reload=False, log_level="info")


def run_test_command():
    print_banner("Running Automated Test Suite")
    python_exe = sys.executable
    cmd = [python_exe, "-m", "pytest", "-v"]
    res = subprocess.run(cmd)
    sys.exit(res.returncode)


def main():
    parser = argparse.ArgumentParser(
        description="Dependency-Aware Parallel Task Orchestration Engine CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Build sub-command
    build_p = subparsers.add_parser("build", help="Execute build pipeline")
    build_p.add_argument("--target", default="all", help="Target output or alias (default: all)")
    build_p.add_argument("--workers", "-j", type=int, default=4, help="Worker limit (default: 4)")
    build_p.add_argument("--clean", action="store_true", help="Clean artifacts before build")

    # Benchmark sub-command
    bench_p = subparsers.add_parser("benchmark", help="Run multi-trial reproducible benchmarks")
    bench_p.add_argument("--trials", type=int, default=3, help="Number of trials per scenario (default: 3)")

    # Dashboard sub-command
    dash_p = subparsers.add_parser("dashboard", help="Start web dashboard and WebSocket telemetry server")
    dash_p.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    dash_p.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")

    # History sub-command
    hist_p = subparsers.add_parser("history", help="View past build executions")
    hist_p.add_argument("--limit", type=int, default=10, help="Max entries to show (default: 10)")

    # Test sub-command
    subparsers.add_parser("test", help="Run full pytest test suite")

    args = parser.parse_args()

    if args.command == "build":
        asyncio.run(run_build_command(target=args.target, workers=args.workers, clean=args.clean))
    elif args.command == "benchmark":
        from benchmarks.run_reproducible_benchmark import run_benchmark_suite
        asyncio.run(run_benchmark_suite(trials=args.trials))
    elif args.command == "dashboard":
        run_dashboard_command(port=args.port, host=args.host)
    elif args.command == "history":
        run_history_command(limit=args.limit)
    elif args.command == "test":
        run_test_command()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
