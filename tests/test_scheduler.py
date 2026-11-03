"""
Tests for E3 — TopologicalScheduler & Build Orchestration.

Validates:
 1. Dependency ordering: prerequisites execute before dependents.
 2. Independent tasks execute concurrently.
 3. Worker limit is respected (max_workers=2).
 4. jobs=1 produces strictly sequential execution.
 5. Up-to-date tasks are skipped without executing commands.
 6. Order-only prerequisites are respected for execution order.
 7. Failed tasks block downstream dependents.
 8. Independent branches continue executing when another branch fails.
 9. Non-zero exit codes lead to FAILED status.
10. Missing outputs prevent successful cache trace recording.
11. Multiple outputs are validated on disk.
12. Target-specific builds execute only the requested closure.
13. Phony aliases never execute commands.
14. Full end-to-end integration on real CMake project.
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from engine.cache import BuildCache
from engine.dag import BuildGraph
from engine.executor import AsyncProcessExecutor
from engine.models import DependencyType, TaskNode
from engine.ninja_parser import NinjaParser
from engine.orchestrator import Orchestrator
from engine.rebuilder import Rebuilder
from engine.scheduler import BuildResult, TaskStatus, TopologicalScheduler

PYTHON = sys.executable


@pytest.fixture
def test_setup(tmp_path):
    """Provides a fresh cache, graph, and rebuilder in a temp directory."""
    db_path = str(tmp_path / "sched_test.db")
    cache = BuildCache(db_path=db_path)
    base_dir = str(tmp_path)
    graph = BuildGraph(base_dir=base_dir)
    rebuilder = Rebuilder(cache=cache, base_dir=base_dir)
    yield tmp_path, cache, graph, rebuilder
    cache.close()


# ─────────────────────────────────────────────────────────────
# 1. Dependency ordering
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dependency_ordering(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    # Task A produces a.txt
    cmd_a = f'"{PYTHON}" -c "open(r\'{tmp_path / "a.txt"}\', \'w\').write(\'A\'); import time; time.sleep(0.05)"'
    # Task B requires a.txt and produces b.txt
    cmd_b = f'"{PYTHON}" -c "open(r\'{tmp_path / "b.txt"}\', \'w\').write(\'B\')"'

    task_a = TaskNode(task_id="A", rule_name="r", command=cmd_a, outputs=["a.txt"])
    task_b = TaskNode(task_id="B", rule_name="r", command=cmd_b, outputs=["b.txt"], explicit_inputs=["a.txt"])

    graph.add_task(task_a)
    graph.add_task(task_b)
    graph.add_dependency("B", "A", DependencyType.EXPLICIT)

    scheduler = TopologicalScheduler(graph, rebuilder, max_workers=2)
    res = await scheduler.build()

    assert res.success is True
    assert res.task_records["A"].status == TaskStatus.COMPLETED
    assert res.task_records["B"].status == TaskStatus.COMPLETED
    # A must finish before B starts
    assert res.task_records["A"].end_time <= res.task_records["B"].start_time


# ─────────────────────────────────────────────────────────────
# 2. Independent tasks execute concurrently
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_independent_tasks_execute_concurrently(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    for i in range(3):
        out_f = tmp_path / f"out_{i}.txt"
        cmd = f'"{PYTHON}" -c "import time; time.sleep(0.1); open(r\'{out_f}\', \'w\').write(\'{i}\')"'
        graph.add_task(TaskNode(task_id=f"T{i}", rule_name="t", command=cmd, outputs=[f"out_{i}.txt"]))

    scheduler = TopologicalScheduler(graph, rebuilder, max_workers=3)
    res = await scheduler.build()

    assert res.success is True
    assert res.completed_count == 3
    # Peak concurrency should be >= 2
    assert res.peak_concurrency >= 2


# ─────────────────────────────────────────────────────────────
# 3. Worker limit is respected
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_limit_strictly_enforced(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    for i in range(4):
        out_f = tmp_path / f"w_{i}.txt"
        cmd = f'"{PYTHON}" -c "import time; time.sleep(0.1); open(r\'{out_f}\', \'w\').write(\'{i}\')"'
        graph.add_task(TaskNode(task_id=f"W{i}", rule_name="w", command=cmd, outputs=[f"w_{i}.txt"]))

    # max_workers = 2
    scheduler = TopologicalScheduler(graph, rebuilder, max_workers=2)
    res = await scheduler.build()

    assert res.success is True
    assert res.peak_concurrency <= 2


# ─────────────────────────────────────────────────────────────
# 4. jobs=1 produces sequential execution
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sequential_execution_jobs_1(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    for i in range(3):
        out_f = tmp_path / f"seq_{i}.txt"
        cmd = f'"{PYTHON}" -c "import time; time.sleep(0.05); open(r\'{out_f}\', \'w\').write(\'{i}\')"'
        graph.add_task(TaskNode(task_id=f"S{i}", rule_name="s", command=cmd, outputs=[f"seq_{i}.txt"]))

    scheduler = TopologicalScheduler(graph, rebuilder, max_workers=1)
    res = await scheduler.build()

    assert res.success is True
    assert res.peak_concurrency == 1


# ─────────────────────────────────────────────────────────────
# 5. Up-to-date tasks are skipped
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_up_to_date_tasks_are_skipped(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    src = tmp_path / "foo.cpp"
    src.write_text("int foo() {}")
    out = tmp_path / "foo.o"
    cmd = f'"{PYTHON}" -c "open(r\'{out}\', \'w\').write(\'OBJ\')"'

    task = TaskNode(task_id="foo.o", rule_name="cxx", command=cmd, outputs=["foo.o"], explicit_inputs=["foo.cpp"])
    graph.add_task(task)

    # First build: executes
    scheduler1 = TopologicalScheduler(graph, rebuilder, max_workers=2)
    res1 = await scheduler1.build()
    assert res1.completed_count == 1
    assert res1.skipped_count == 0

    # Second build without changes: skips!
    scheduler2 = TopologicalScheduler(graph, rebuilder, max_workers=2)
    res2 = await scheduler2.build()
    assert res2.completed_count == 0
    assert res2.skipped_count == 1
    assert res2.task_records["foo.o"].status == TaskStatus.SKIPPED


# ─────────────────────────────────────────────────────────────
# 6. Order-only prerequisites are respected for execution order
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_only_enforces_execution_order(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    # Task OrderGen produces a flag file
    flag = tmp_path / "flag.txt"
    cmd_gen = f'"{PYTHON}" -c "import time; time.sleep(0.05); open(r\'{flag}\', \'w\').write(\'READY\')"'
    # Task Main requires that flag exists before starting
    cmd_main = f'"{PYTHON}" -c "assert open(r\'{flag}\').read() == \'READY\'; open(r\'{tmp_path / "main.out"}\', \'w\').write(\'OK\')"'

    t_gen = TaskNode(task_id="gen", rule_name="r", command=cmd_gen, outputs=["flag.txt"])
    t_main = TaskNode(task_id="main", rule_name="r", command=cmd_main, outputs=["main.out"], order_only_inputs=["flag.txt"])

    graph.add_task(t_gen)
    graph.add_task(t_main)
    graph.add_dependency("main", "gen", DependencyType.ORDER_ONLY)

    scheduler = TopologicalScheduler(graph, rebuilder, max_workers=2)
    res = await scheduler.build()

    assert res.success is True
    assert res.task_records["gen"].end_time <= res.task_records["main"].start_time


# ─────────────────────────────────────────────────────────────
# 7. Failed tasks block downstream dependents
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_task_blocks_downstream(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    cmd_fail = f'"{PYTHON}" -c "import sys; sys.exit(1)"'
    cmd_down = f'"{PYTHON}" -c "open(r\'{tmp_path / "down.txt"}\', \'w\').write(\'D\')"'

    t_fail = TaskNode(task_id="fail", rule_name="r", command=cmd_fail, outputs=["fail.out"])
    t_down = TaskNode(task_id="down", rule_name="r", command=cmd_down, outputs=["down.txt"], explicit_inputs=["fail.out"])

    graph.add_task(t_fail)
    graph.add_task(t_down)
    graph.add_dependency("down", "fail", DependencyType.EXPLICIT)

    scheduler = TopologicalScheduler(graph, rebuilder, max_workers=2)
    res = await scheduler.build()

    assert res.success is False
    assert res.task_records["fail"].status == TaskStatus.FAILED
    assert res.task_records["down"].status == TaskStatus.BLOCKED
    assert res.failed_count == 1
    assert res.blocked_count == 1
    # Downstream command never ran
    assert not (tmp_path / "down.txt").exists()


# ─────────────────────────────────────────────────────────────
# 8. Independent branches continue after failure
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_independent_branch_continues_after_failure(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    # Branch 1: fails
    cmd_fail = f'"{PYTHON}" -c "import sys; sys.exit(1)"'
    t_fail = TaskNode(task_id="branch1_fail", rule_name="r", command=cmd_fail, outputs=["b1.out"])

    # Branch 2: succeeds
    out2 = tmp_path / "b2.out"
    cmd_ok = f'"{PYTHON}" -c "open(r\'{out2}\', \'w\').write(\'OK\')"'
    t_ok = TaskNode(task_id="branch2_ok", rule_name="r", command=cmd_ok, outputs=["b2.out"])

    graph.add_task(t_fail)
    graph.add_task(t_ok)

    scheduler = TopologicalScheduler(graph, rebuilder, max_workers=2)
    res = await scheduler.build()

    assert res.task_records["branch1_fail"].status == TaskStatus.FAILED
    # Independent branch 2 must succeed despite branch 1 failing!
    assert res.task_records["branch2_ok"].status == TaskStatus.COMPLETED
    assert out2.exists()


# ─────────────────────────────────────────────────────────────
# 9. Non-zero exit code produces FAILED status
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nonzero_exit_code_status(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    t = TaskNode(task_id="bad", rule_name="r", command=f'"{PYTHON}" -c "import sys; sys.exit(5)"')
    graph.add_task(t)

    scheduler = TopologicalScheduler(graph, rebuilder)
    res = await scheduler.build()

    assert res.success is False
    assert res.task_records["bad"].status == TaskStatus.FAILED
    assert res.task_records["bad"].return_code == 5


# ─────────────────────────────────────────────────────────────
# 10. Missing outputs prevent successful cache recording
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_output_prevents_cache_recording(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    # Exits with 0 but forgets to create declared output
    t = TaskNode(task_id="phantom", rule_name="r", command=f'"{PYTHON}" -c "print(\'Done\')"', outputs=["phantom.bin"])
    graph.add_task(t)

    scheduler = TopologicalScheduler(graph, rebuilder)
    res = await scheduler.build()

    assert res.success is False
    assert res.task_records["phantom"].status == TaskStatus.FAILED
    # SQLite cache must NOT contain a trace for this task!
    assert cache.get_trace("phantom") is None


# ─────────────────────────────────────────────────────────────
# 11. Multiple outputs are validated
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_outputs_validated(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    o1 = tmp_path / "x.h"
    o2 = tmp_path / "x.cpp"
    cmd = f'"{PYTHON}" -c "open(r\'{o1}\', \'w\').write(\'H\'); open(r\'{o2}\', \'w\').write(\'C\')"'

    t = TaskNode(task_id="codegen", rule_name="r", command=cmd, outputs=["x.h", "x.cpp"])
    graph.add_task(t)

    scheduler = TopologicalScheduler(graph, rebuilder)
    res = await scheduler.build()

    assert res.success is True
    assert res.task_records["codegen"].status == TaskStatus.COMPLETED
    assert o1.exists() and o2.exists()


# ─────────────────────────────────────────────────────────────
# 12. Target-specific builds execute only requested closure
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_target_specific_closure_execution(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    out_a = tmp_path / "a.out"
    out_b = tmp_path / "b.out"
    cmd_a = f'"{PYTHON}" -c "open(r\'{out_a}\', \'w\').write(\'A\')"'
    cmd_b = f'"{PYTHON}" -c "open(r\'{out_b}\', \'w\').write(\'B\')"'

    t_a = TaskNode(task_id="target_A", rule_name="r", command=cmd_a, outputs=["a.out"])
    t_b = TaskNode(task_id="target_B", rule_name="r", command=cmd_b, outputs=["b.out"])

    graph.add_task(t_a)
    graph.add_task(t_b)

    # Build target A only
    scheduler = TopologicalScheduler(graph, rebuilder)
    res = await scheduler.build(target="target_A")

    assert res.success is True
    assert res.total_tasks == 1
    assert "target_A" in res.task_records
    assert "target_B" not in res.task_records
    assert out_a.exists()
    assert not out_b.exists()


# ─────────────────────────────────────────────────────────────
# 13. Phony aliases never execute commands
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phony_aliases_do_not_execute(test_setup):
    tmp_path, cache, graph, rebuilder = test_setup

    out_real = tmp_path / "real.out"
    cmd_real = f'"{PYTHON}" -c "open(r\'{out_real}\', \'w\').write(\'REAL\')"'
    t_real = TaskNode(task_id="real.out", rule_name="cxx", command=cmd_real, outputs=["real.out"])

    graph.add_task(t_real)
    graph.phony_targets["alias"] = [("real.out", DependencyType.EXPLICIT)]

    scheduler = TopologicalScheduler(graph, rebuilder)
    res = await scheduler.build(target="alias")

    assert res.success is True
    assert res.total_tasks == 1
    assert "real.out" in res.task_records
    assert "alias" not in res.task_records
    assert out_real.exists()


# ─────────────────────────────────────────────────────────────
# 14. Real CMake C++ demo project end-to-end execution
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_real_cmake_demo_build_end_to_end(tmp_path):
    manifest_path = os.path.join(
        PROJECT_ROOT, "examples", "cpp_demo", "build", "build.ninja"
    )
    if not os.path.isfile(manifest_path):
        pytest.skip("CMake build.ninja not found.")

    db_path = str(tmp_path / "demo_orchestrator.db")
    base_dir = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "build")

    orchestrator = Orchestrator(
        manifest_path=manifest_path,
        db_path=db_path,
        base_dir=base_dir,
        max_workers=4,
    )

    try:
        # 1. Clean parallel build
        res1 = await orchestrator.build(target="all")
        assert res1.success is True
        assert res1.completed_count == 9
        assert res1.failed_count == 0

        # Verify binaries were actually produced by GCC!
        demo_exe = os.path.join(base_dir, "demo.exe")
        test_runner_exe = os.path.join(base_dir, "test_runner.exe")
        lib_a = os.path.join(base_dir, "libmylib.a")

        assert os.path.isfile(demo_exe), "demo.exe was not created!"
        assert os.path.isfile(test_runner_exe), "test_runner.exe was not created!"
        assert os.path.isfile(lib_a), "libmylib.a was not created!"

        # 2. No-op build (all 9 tasks should skip)
        res2 = await orchestrator.build(target="all")
        assert res2.success is True
        assert res2.completed_count == 0
        assert res2.skipped_count == 9
    finally:
        orchestrator.close()
