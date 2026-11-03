"""
Comprehensive End-to-End Functional Test Suite (14 Scenarios)

Validates all 14 critical verification scenarios on the real C++ project:
 1. Fresh clean build
 2. Sequential build (1 worker)
 3. Parallel build (4 workers)
 4. No-op build
 5. Incremental source modification
 6. Shared-header modification
 7. Deleted output file
 8. Externally modified output
 9. Compilation failure
10. Downstream task blocking
11. Independent branch continuation
12. Historical build retrieval
13. Live WebSocket telemetry
14. Dashboard refresh and reconnection
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
import pytest
from starlette.testclient import TestClient

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from api.main import app, clean_artifacts, DEFAULT_BASE_DIR, DEFAULT_MANIFEST
from engine.cache import BuildCache
from engine.orchestrator import Orchestrator
from engine.scheduler import TaskStatus
from telemetry.collector import TelemetryCollector
from telemetry.db import TelemetryDB
from telemetry.event_bus import EventBus
from telemetry.events import TelemetryEventType


@pytest.fixture(scope="module")
def setup_clean_env():
    """Ensure cpp_demo build is cleanly configured before tests."""
    clean_artifacts(DEFAULT_BASE_DIR)
    yield
    clean_artifacts(DEFAULT_BASE_DIR)


# ─────────────────────────────────────────────────────────────
# 1. Fresh clean build & 2. Sequential build (1 worker)
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_01_02_fresh_clean_and_sequential_build(tmp_path):
    db_p = str(tmp_path / "seq.db")
    clean_artifacts(DEFAULT_BASE_DIR, db_path=db_p)

    orch = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=1)
    res = await orch.build(target="all")
    orch.close()

    assert res.success is True
    assert res.completed_count == 9
    assert res.skipped_count == 0
    assert res.peak_concurrency == 1
    assert os.path.isfile(os.path.join(DEFAULT_BASE_DIR, "demo.exe"))
    assert os.path.isfile(os.path.join(DEFAULT_BASE_DIR, "test_runner.exe"))
    assert os.path.isfile(os.path.join(DEFAULT_BASE_DIR, "libmylib.a"))


# ─────────────────────────────────────────────────────────────
# 3. Parallel build using four workers
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_03_parallel_build_four_workers(tmp_path):
    db_p = str(tmp_path / "par.db")
    clean_artifacts(DEFAULT_BASE_DIR, db_path=db_p)

    orch = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
    res = await orch.build(target="all")
    orch.close()

    assert res.success is True
    assert res.completed_count == 9
    assert res.peak_concurrency >= 2
    assert os.path.isfile(os.path.join(DEFAULT_BASE_DIR, "demo.exe"))


# ─────────────────────────────────────────────────────────────
# 4. No-op build
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_04_noop_build(tmp_path):
    db_p = str(tmp_path / "noop.db")
    clean_artifacts(DEFAULT_BASE_DIR, db_path=db_p)

    # Initial build
    orch1 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
    res1 = await orch1.build(target="all")
    orch1.close()
    assert res1.completed_count == 9

    # Immediate second build without modifying anything
    orch2 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
    res2 = await orch2.build(target="all")
    orch2.close()

    assert res2.success is True
    assert res2.completed_count == 0
    assert res2.skipped_count == 9
    assert res2.duration < 0.5  # Fast no-op decision


# ─────────────────────────────────────────────────────────────
# 5. Incremental source modification
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_05_incremental_source_modification(tmp_path):
    db_p = str(tmp_path / "inc.db")
    clean_artifacts(DEFAULT_BASE_DIR, db_path=db_p)

    # Initial build
    orch1 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
    await orch1.build(target="all")
    orch1.close()

    # Modify math.cpp
    math_cpp = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "src", "math.cpp")
    with open(math_cpp, "r", encoding="utf-8") as f:
        original_code = f.read()

    try:
        with open(math_cpp, "a", encoding="utf-8") as f:
            f.write("\n// incremental modification test\n")

        orch2 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
        res2 = await orch2.build(target="all")
        orch2.close()

        assert res2.success is True
        # math.cpp.obj must be rebuilt
        math_tid = [tid for tid in res2.task_records if "math.cpp.obj" in tid][0]
        assert res2.task_records[math_tid].status == TaskStatus.COMPLETED

        # utils, stringops, fileio, main, test_runner object compilations must be skipped
        utils_tid = [tid for tid in res2.task_records if "utils.cpp.obj" in tid][0]
        assert res2.task_records[utils_tid].status == TaskStatus.SKIPPED
    finally:
        with open(math_cpp, "w", encoding="utf-8") as f:
            f.write(original_code)


# ─────────────────────────────────────────────────────────────
# 6. Shared-header modification
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_06_shared_header_modification(tmp_path):
    db_p = str(tmp_path / "hdr.db")
    clean_artifacts(DEFAULT_BASE_DIR, db_path=db_p)

    orch1 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
    await orch1.build(target="all")
    orch1.close()

    # math.h is included by math.cpp, demo/main.cpp, and test_runner.cpp
    math_h = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "src", "math.h")
    with open(math_h, "r", encoding="utf-8") as f:
        original_hdr = f.read()

    try:
        with open(math_h, "a", encoding="utf-8") as f:
            f.write("\n// shared header touch\n")

        orch2 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
        res2 = await orch2.build(target="all")
        orch2.close()

        assert res2.success is True
        # math.cpp.obj should rebuild
        math_tid = [tid for tid in res2.task_records if "math.cpp.obj" in tid][0]
        assert res2.task_records[math_tid].status == TaskStatus.COMPLETED

        # utils.cpp.obj does NOT include math.h -> must be SKIPPED
        utils_tid = [tid for tid in res2.task_records if "utils.cpp.obj" in tid][0]
        assert res2.task_records[utils_tid].status == TaskStatus.SKIPPED
    finally:
        with open(math_h, "w", encoding="utf-8") as f:
            f.write(original_hdr)


# ─────────────────────────────────────────────────────────────
# 7. Deleted output file
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_07_deleted_output_file(tmp_path):
    db_p = str(tmp_path / "del_out.db")
    clean_artifacts(DEFAULT_BASE_DIR, db_path=db_p)

    orch1 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
    await orch1.build(target="all")
    orch1.close()

    # Delete demo.exe manually
    demo_exe = os.path.join(DEFAULT_BASE_DIR, "demo.exe")
    assert os.path.isfile(demo_exe)
    os.remove(demo_exe)

    orch2 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
    res2 = await orch2.build(target="all")
    orch2.close()

    assert res2.success is True
    # demo.exe task must rebuild because output was missing on disk
    demo_tid = [tid for tid in res2.task_records if "demo.exe" in tid and "main" not in tid][0]
    assert res2.task_records[demo_tid].status == TaskStatus.COMPLETED
    assert os.path.isfile(demo_exe)

    # Object files remain up-to-date
    utils_tid = [tid for tid in res2.task_records if "utils.cpp.obj" in tid][0]
    assert res2.task_records[utils_tid].status == TaskStatus.SKIPPED


# ─────────────────────────────────────────────────────────────
# 8. Externally modified output
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_08_externally_modified_output(tmp_path):
    db_p = str(tmp_path / "mod_out.db")
    clean_artifacts(DEFAULT_BASE_DIR, db_path=db_p)

    orch1 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
    await orch1.build(target="all")
    orch1.close()

    # Tamper with demo.exe contents externally
    demo_exe = os.path.join(DEFAULT_BASE_DIR, "demo.exe")
    with open(demo_exe, "wb") as f:
        f.write(b"CORRUPTED_BIN_HEADER")

    orch2 = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
    res2 = await orch2.build(target="all")
    orch2.close()

    assert res2.success is True
    demo_tid = [tid for tid in res2.task_records if "demo.exe" in tid and "main" not in tid][0]
    # Rebuilder detects hash mismatch and rebuilds demo.exe
    assert res2.task_records[demo_tid].status == TaskStatus.COMPLETED
    # File is restored to a valid executable
    assert os.path.getsize(demo_exe) > 1000


# ─────────────────────────────────────────────────────────────
# 9. Compilation failure, 10. Downstream blocking, 11. Independent branch continuation
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_09_10_11_compilation_failure_blocking_and_independent_continuation(tmp_path):
    db_p = str(tmp_path / "fail.db")
    clean_artifacts(DEFAULT_BASE_DIR, db_path=db_p)

    math_cpp = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "src", "math.cpp")
    with open(math_cpp, "r", encoding="utf-8") as f:
        original_code = f.read()

    try:
        # Inject deliberate compilation error
        with open(math_cpp, "w", encoding="utf-8") as f:
            f.write("#error Deliberate Compilation Error For Testing\n" + original_code)

        orch = Orchestrator(DEFAULT_MANIFEST, db_path=db_p, base_dir=DEFAULT_BASE_DIR, max_workers=4)
        res = await orch.build(target="all")
        orch.close()

        assert res.success is False

        # 9. math.cpp.obj must be FAILED
        math_tid = [tid for tid in res.task_records if "math.cpp.obj" in tid][0]
        assert res.task_records[math_tid].status == TaskStatus.FAILED

        # 10. Downstream tasks (libmylib.a, demo.exe, test_runner.exe) must be BLOCKED
        lib_tid = [tid for tid in res.task_records if "libmylib.a" in tid][0]
        demo_tid = [tid for tid in res.task_records if "demo.exe" in tid and "main" not in tid][0]
        test_tid = [tid for tid in res.task_records if "test_runner.exe" in tid and "test_runner.cpp" not in tid][0]

        assert res.task_records[lib_tid].status == TaskStatus.BLOCKED
        assert res.task_records[demo_tid].status == TaskStatus.BLOCKED
        assert res.task_records[test_tid].status == TaskStatus.BLOCKED

        # 11. Independent branch tasks (e.g. main.cpp.obj, test_runner.cpp.obj, utils.cpp.obj)
        # must have continued executing and completed successfully!
        main_obj_tid = [tid for tid in res.task_records if "main.cpp.obj" in tid][0]
        utils_obj_tid = [tid for tid in res.task_records if "utils.cpp.obj" in tid][0]
        assert res.task_records[main_obj_tid].status == TaskStatus.COMPLETED
        assert res.task_records[utils_obj_tid].status == TaskStatus.COMPLETED

    finally:
        with open(math_cpp, "w", encoding="utf-8") as f:
            f.write(original_code)


# ─────────────────────────────────────────────────────────────
# 12. Historical build retrieval
# ─────────────────────────────────────────────────────────────

def test_12_historical_build_retrieval():
    client = TestClient(app)
    res = client.get("/api/builds")
    assert res.status_code == 200
    builds = res.json()
    assert isinstance(builds, list)
    if builds:
        first_id = builds[0]["build_id"]
        detail = client.get(f"/api/builds/{first_id}")
        assert detail.status_code == 200
        d_json = detail.json()
        assert "summary" in d_json
        assert "events" in d_json


# ─────────────────────────────────────────────────────────────
# 13. Live WebSocket telemetry
# ─────────────────────────────────────────────────────────────

def test_13_live_websocket_telemetry():
    client = TestClient(app)
    with client.websocket_connect("/ws/telemetry") as ws:
        init_data = ws.receive_json()
        assert init_data["type"] == "INIT"
        assert "wall_clock_duration" in init_data["data"]

        ws.send_text("ping")
        assert ws.receive_text() == "pong"


# ─────────────────────────────────────────────────────────────
# 14. Dashboard refresh and reconnection
# ─────────────────────────────────────────────────────────────

def test_14_dashboard_refresh_and_reconnection():
    client = TestClient(app)

    # Initial connect
    with client.websocket_connect("/ws/telemetry") as ws1:
        msg1 = ws1.receive_json()
        assert msg1["type"] == "INIT"

    # Reconnect immediately
    with client.websocket_connect("/ws/telemetry") as ws2:
        msg2 = ws2.receive_json()
        assert msg2["type"] == "INIT"

    # Status and graph endpoints refresh cleanly
    res_status = client.get("/api/status")
    assert res_status.status_code == 200
    res_graph = client.get("/api/graph?target=all")
    assert res_graph.status_code == 200
    assert len(res_graph.json()["nodes"]) == 9
