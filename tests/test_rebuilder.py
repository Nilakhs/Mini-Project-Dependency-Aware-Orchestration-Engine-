"""
Tests for E2 — Verifying-Trace Rebuilder.

Validates all 20 scenarios required by Phase 3:
 1. First build requires execution (BUILD: FIRST_BUILD).
 2. Identical second build can skip (SKIP: UP_TO_DATE).
 3. Explicit input content change triggers rebuild.
 4. Implicit input content change triggers rebuild.
 5. Order-only content change does not independently trigger rebuild.
 6. Command change triggers rebuild.
 7. Rule variable change triggers rebuild.
 8. Missing output triggers rebuild.
 9. Externally modified output triggers rebuild.
10. Missing input triggers a safe rebuild/error.
11. Dependency fingerprint changes propagate downstream.
12. Unrelated tasks remain up-to-date.
13. Failed task does not update successful trace.
14. SQLite records persist across engine restarts.
15. Multiple outputs are validated.
16. Corrupted cache is handled safely.
17. Clean build followed by no-op build.
18. Incremental source modification.
19. Shared header modification (depfile tracking).
20. Real CMake manifest integration.
"""

import os
import sys
import tempfile
import time
from pathlib import Path
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from engine.cache import BuildCache, BuildTrace, CacheError
from engine.dag import BuildGraph
from engine.models import BuildEdge, BuildRule, DependencyType, EdgeType, NinjaManifest, TaskNode
from engine.ninja_parser import NinjaParser
from engine.rebuilder import (
    RebuildAction,
    RebuildDecision,
    RebuildReason,
    Rebuilder,
    hash_file,
    hash_string,
    parse_depfile,
)


@pytest.fixture
def tmp_env(tmp_path):
    """Provides a clean temporary directory with a cache and base graph."""
    db_path = str(tmp_path / "test_cache.db")
    cache = BuildCache(db_path=db_path)
    base_dir = str(tmp_path)
    graph = BuildGraph(base_dir=base_dir)
    rebuilder = Rebuilder(cache=cache, base_dir=base_dir)
    yield tmp_path, cache, graph, rebuilder
    cache.close()


# ─────────────────────────────────────────────────────────────
# 1. First build requires execution
# ─────────────────────────────────────────────────────────────

def test_first_build_requires_execution(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "foo.cpp"
    src.write_text("int foo() { return 1; }")
    out = tmp_path / "foo.o"

    task = TaskNode(
        task_id="foo.o",
        rule_name="cxx",
        command="g++ -c foo.cpp -o foo.o",
        outputs=["foo.o"],
        explicit_inputs=["foo.cpp"],
    )
    graph.add_task(task)

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD
    assert decision.reason == RebuildReason.FIRST_BUILD


# ─────────────────────────────────────────────────────────────
# 2. Identical second build can skip
# ─────────────────────────────────────────────────────────────

def test_identical_second_build_skips(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "foo.cpp"
    src.write_text("int foo() { return 1; }")
    out = tmp_path / "foo.o"
    out.write_text("BINARY_OBJECT_CONTENT")

    task = TaskNode(
        task_id="foo.o",
        rule_name="cxx",
        command="g++ -c foo.cpp -o foo.o",
        outputs=["foo.o"],
        explicit_inputs=["foo.cpp"],
    )
    graph.add_task(task)

    # Record successful execution trace
    rebuilder.record_success(task, graph)

    # Re-evaluate
    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.SKIP
    assert decision.reason == RebuildReason.UP_TO_DATE


# ─────────────────────────────────────────────────────────────
# 3. Explicit input content change triggers rebuild
# ─────────────────────────────────────────────────────────────

def test_explicit_input_change_triggers_rebuild(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "foo.cpp"
    src.write_text("int foo() { return 1; }")
    out = tmp_path / "foo.o"
    out.write_text("BINARY_OBJECT_CONTENT")

    task = TaskNode(
        task_id="foo.o",
        rule_name="cxx",
        command="g++ -c foo.cpp -o foo.o",
        outputs=["foo.o"],
        explicit_inputs=["foo.cpp"],
    )
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Modify explicit input file
    src.write_text("int foo() { return 2; /* CHANGED */ }")

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD
    assert decision.reason == RebuildReason.INPUT_CHANGED


# ─────────────────────────────────────────────────────────────
# 4. Implicit input content change triggers rebuild
# ─────────────────────────────────────────────────────────────

def test_implicit_input_change_triggers_rebuild(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "main.o"
    src.write_text("OBJ")
    lib = tmp_path / "libfoo.a"
    lib.write_text("LIB_ARCHIVE_V1")
    exe = tmp_path / "app.exe"
    exe.write_text("EXE_CONTENT")

    task = TaskNode(
        task_id="app.exe",
        rule_name="link",
        command="g++ main.o -lfoo -o app.exe",
        outputs=["app.exe"],
        explicit_inputs=["main.o"],
        implicit_inputs=["libfoo.a"],
    )
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Modify implicit input (library archive)
    lib.write_text("LIB_ARCHIVE_V2_CHANGED")

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD
    assert decision.reason == RebuildReason.INPUT_CHANGED


# ─────────────────────────────────────────────────────────────
# 5. Order-only content change does not trigger rebuild
# ─────────────────────────────────────────────────────────────

def test_order_only_change_does_not_trigger_rebuild(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "main.cpp"
    src.write_text("int main() { return 0; }")
    dummy = tmp_path / "order_flag"
    dummy.write_text("V1")
    out = tmp_path / "main.o"
    out.write_text("OBJ_DATA")

    task = TaskNode(
        task_id="main.o",
        rule_name="cxx",
        command="g++ -c main.cpp -o main.o",
        outputs=["main.o"],
        explicit_inputs=["main.cpp"],
        order_only_inputs=["order_flag"],
    )
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Change order-only input content
    dummy.write_text("V2_CHANGED_ORDER_ONLY")

    # Should STILL SKIP because order-only inputs do not trigger rebuilds!
    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.SKIP
    assert decision.reason == RebuildReason.UP_TO_DATE


# ─────────────────────────────────────────────────────────────
# 6. Command change triggers rebuild
# ─────────────────────────────────────────────────────────────

def test_command_change_triggers_rebuild(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "foo.cpp"
    src.write_text("int foo() { return 1; }")
    out = tmp_path / "foo.o"
    out.write_text("OBJ")

    task = TaskNode(
        task_id="foo.o",
        rule_name="cxx",
        command="g++ -O2 -c foo.cpp -o foo.o",
        outputs=["foo.o"],
        explicit_inputs=["foo.cpp"],
    )
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Change optimization flag in command
    task.command = "g++ -O3 -c foo.cpp -o foo.o"

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD
    assert decision.reason == RebuildReason.COMMAND_CHANGED


# ─────────────────────────────────────────────────────────────
# 7. Rule variable change triggers rebuild
# ─────────────────────────────────────────────────────────────

def test_variable_change_triggers_rebuild(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "foo.cpp"
    src.write_text("int foo() { return 1; }")
    out = tmp_path / "foo.o"
    out.write_text("OBJ")

    task = TaskNode(
        task_id="foo.o",
        rule_name="cxx",
        command="g++ -c foo.cpp -o foo.o",
        outputs=["foo.o"],
        explicit_inputs=["foo.cpp"],
        variables={"FLAGS": "-DDEBUG"},
    )
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Modify edge variable
    task.variables["FLAGS"] = "-DNDEBUG"

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD
    assert decision.reason == RebuildReason.COMMAND_CHANGED


# ─────────────────────────────────────────────────────────────
# 8. Missing output triggers rebuild
# ─────────────────────────────────────────────────────────────

def test_missing_output_triggers_rebuild(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "foo.cpp"
    src.write_text("int foo() { return 1; }")
    out = tmp_path / "foo.o"
    out.write_text("OBJ")

    task = TaskNode(
        task_id="foo.o",
        rule_name="cxx",
        command="g++ -c foo.cpp -o foo.o",
        outputs=["foo.o"],
        explicit_inputs=["foo.cpp"],
    )
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Delete output file
    os.remove(str(out))

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD
    assert decision.reason == RebuildReason.OUTPUT_MISSING


# ─────────────────────────────────────────────────────────────
# 9. Externally modified output triggers rebuild
# ─────────────────────────────────────────────────────────────

def test_externally_modified_output_triggers_rebuild(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "foo.cpp"
    src.write_text("int foo() { return 1; }")
    out = tmp_path / "foo.o"
    out.write_text("ORIGINAL_OBJ")

    task = TaskNode(
        task_id="foo.o",
        rule_name="cxx",
        command="g++ -c foo.cpp -o foo.o",
        outputs=["foo.o"],
        explicit_inputs=["foo.cpp"],
    )
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Tamper with output file
    out.write_text("CORRUPTED_TAMPERED_OUTPUT")

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD
    assert decision.reason == RebuildReason.OUTPUT_MODIFIED


# ─────────────────────────────────────────────────────────────
# 10. Missing input triggers safe rebuild
# ─────────────────────────────────────────────────────────────

def test_missing_input_handled_safely(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    task = TaskNode(
        task_id="bar.o",
        rule_name="cxx",
        command="g++ -c nonexistent.cpp -o bar.o",
        outputs=["bar.o"],
        explicit_inputs=["nonexistent.cpp"],
    )
    graph.add_task(task)
    graph.source_files.add("nonexistent.cpp")

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD


# ─────────────────────────────────────────────────────────────
# 11. Dependency fingerprint changes propagate
# ─────────────────────────────────────────────────────────────

def test_dependency_change_propagates_downstream(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "math.cpp"
    src.write_text("int add(int a, int b) { return a + b; }")
    obj = tmp_path / "math.o"
    obj.write_text("MATH_OBJ")
    exe = tmp_path / "app.exe"
    exe.write_text("APP_EXE")

    t_compile = TaskNode(
        task_id="math.o",
        rule_name="cxx",
        command="g++ -c math.cpp -o math.o",
        outputs=["math.o"],
        explicit_inputs=["math.cpp"],
    )
    t_link = TaskNode(
        task_id="app.exe",
        rule_name="link",
        command="g++ math.o -o app.exe",
        outputs=["app.exe"],
        explicit_inputs=["math.o"],
    )

    graph.add_task(t_compile)
    graph.add_task(t_link)
    graph.add_dependency("app.exe", "math.o", DependencyType.EXPLICIT)

    # Initial success for both
    rebuilder.record_success(t_compile, graph)
    rebuilder.record_success(t_link, graph)

    # Modify math.cpp
    src.write_text("int add(int a, int b) { return a + b + 1; /* MODIFIED */ }")

    # Evaluate full plan in topological order
    plan = rebuilder.evaluate_plan(graph, target="app.exe")

    assert plan["math.o"].action == RebuildAction.BUILD
    assert plan["math.o"].reason == RebuildReason.INPUT_CHANGED

    # Downstream task MUST also rebuild due to upstream change!
    assert plan["app.exe"].action == RebuildAction.BUILD
    assert plan["app.exe"].reason == RebuildReason.DEPENDENCY_CHANGED


# ─────────────────────────────────────────────────────────────
# 12. Unrelated tasks remain up-to-date
# ─────────────────────────────────────────────────────────────

def test_unrelated_tasks_remain_up_to_date(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    # Task A: math.cpp -> math.o
    (tmp_path / "math.cpp").write_text("MATH")
    (tmp_path / "math.o").write_text("MATH_OBJ")
    t_a = TaskNode(task_id="math.o", rule_name="cxx", outputs=["math.o"], explicit_inputs=["math.cpp"])

    # Task B: utils.cpp -> utils.o (unrelated)
    (tmp_path / "utils.cpp").write_text("UTILS")
    (tmp_path / "utils.o").write_text("UTILS_OBJ")
    t_b = TaskNode(task_id="utils.o", rule_name="cxx", outputs=["utils.o"], explicit_inputs=["utils.cpp"])

    graph.add_task(t_a)
    graph.add_task(t_b)
    rebuilder.record_success(t_a, graph)
    rebuilder.record_success(t_b, graph)

    # Modify math.cpp only
    (tmp_path / "math.cpp").write_text("MATH_MODIFIED")

    plan = rebuilder.evaluate_plan(graph)
    assert plan["math.o"].action == RebuildAction.BUILD
    assert plan["utils.o"].action == RebuildAction.SKIP
    assert plan["utils.o"].reason == RebuildReason.UP_TO_DATE


# ─────────────────────────────────────────────────────────────
# 13. Failed task does not update successful trace
# ─────────────────────────────────────────────────────────────

def test_failed_task_does_not_update_trace(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    task = TaskNode(
        task_id="fail.o",
        rule_name="cxx",
        outputs=["fail.o"],
        explicit_inputs=["fail.cpp"],
    )
    graph.add_task(task)

    # Output missing: record_success must raise CacheError
    with pytest.raises(CacheError):
        rebuilder.record_success(task, graph)

    assert cache.get_trace("fail.o") is None


# ─────────────────────────────────────────────────────────────
# 14. SQLite records persist across engine restarts
# ─────────────────────────────────────────────────────────────

def test_cache_persists_across_restarts(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "test.cpp"
    src.write_text("code")
    out = tmp_path / "test.o"
    out.write_text("obj")

    task = TaskNode(task_id="test.o", rule_name="cxx", outputs=["test.o"], explicit_inputs=["test.cpp"])
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Close the cache and reopen a new instance from the same DB file
    cache.close()
    new_cache = BuildCache(db_path=cache.db_path)
    new_rebuilder = Rebuilder(cache=new_cache, base_dir=str(tmp_path))

    decision = new_rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.SKIP
    assert decision.reason == RebuildReason.UP_TO_DATE
    new_cache.close()


# ─────────────────────────────────────────────────────────────
# 15. Multiple outputs are validated
# ─────────────────────────────────────────────────────────────

def test_multiple_outputs_validation(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    (tmp_path / "in.spec").write_text("SPEC")
    out1 = tmp_path / "gen.h"
    out2 = tmp_path / "gen.cpp"
    out1.write_text("HEADER")
    out2.write_text("CPP")

    task = TaskNode(
        task_id="codegen",
        rule_name="gen",
        outputs=["gen.h", "gen.cpp"],
        explicit_inputs=["in.spec"],
    )
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Delete just ONE output
    os.remove(str(out2))

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD
    assert decision.reason == RebuildReason.OUTPUT_MISSING


# ─────────────────────────────────────────────────────────────
# 16. Corrupted cache is handled safely
# ─────────────────────────────────────────────────────────────

def test_corrupted_cache_recovery(tmp_path):
    db_path = str(tmp_path / "corrupt.db")
    with open(db_path, "wb") as f:
        f.write(b"CORRUPTED_NON_SQLITE_HEADER_GARBAGE")

    # Should not crash; automatically recovers
    cache = BuildCache(db_path=db_path)
    assert cache.get_trace("any_task") is None
    cache.close()


# ─────────────────────────────────────────────────────────────
# 17. Clean build followed by no-op build
# ─────────────────────────────────────────────────────────────

def test_clean_build_then_noop_build(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    src = tmp_path / "main.cpp"
    src.write_text("int main() {}")
    out = tmp_path / "main.exe"

    task = TaskNode(task_id="main.exe", rule_name="link", outputs=["main.exe"], explicit_inputs=["main.cpp"])
    graph.add_task(task)

    # 1. First run: Clean build
    plan1 = rebuilder.evaluate_plan(graph)
    assert plan1["main.exe"].action == RebuildAction.BUILD

    # Simulate compilation
    out.write_text("EXECUTABLE_BYTES")
    rebuilder.record_success(task, graph)

    # 2. Second run: No-op build
    plan2 = rebuilder.evaluate_plan(graph)
    assert plan2["main.exe"].action == RebuildAction.SKIP
    assert plan2["main.exe"].reason == RebuildReason.UP_TO_DATE


# ─────────────────────────────────────────────────────────────
# 18. Incremental source modification
# ─────────────────────────────────────────────────────────────

def test_incremental_source_modification(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    # 2 source files, 2 object files, 1 executable
    (tmp_path / "a.cpp").write_text("int a() { return 1; }")
    (tmp_path / "b.cpp").write_text("int b() { return 2; }")
    (tmp_path / "a.o").write_text("OBJ_A")
    (tmp_path / "b.o").write_text("OBJ_B")
    (tmp_path / "app.exe").write_text("EXE")

    t_a = TaskNode(task_id="a.o", rule_name="cxx", outputs=["a.o"], explicit_inputs=["a.cpp"])
    t_b = TaskNode(task_id="b.o", rule_name="cxx", outputs=["b.o"], explicit_inputs=["b.cpp"])
    t_link = TaskNode(task_id="app.exe", rule_name="link", outputs=["app.exe"], explicit_inputs=["a.o", "b.o"])

    graph.add_task(t_a)
    graph.add_task(t_b)
    graph.add_task(t_link)
    graph.add_dependency("app.exe", "a.o", DependencyType.EXPLICIT)
    graph.add_dependency("app.exe", "b.o", DependencyType.EXPLICIT)

    rebuilder.record_success(t_a, graph)
    rebuilder.record_success(t_b, graph)
    rebuilder.record_success(t_link, graph)

    # Modify ONLY a.cpp
    (tmp_path / "a.cpp").write_text("int a() { return 99; /* CHANGED */ }")

    plan = rebuilder.evaluate_plan(graph)
    assert plan["a.o"].action == RebuildAction.BUILD
    assert plan["b.o"].action == RebuildAction.SKIP  # Untouched
    assert plan["app.exe"].action == RebuildAction.BUILD  # Depends on a.o


# ─────────────────────────────────────────────────────────────
# 19. Shared header modification (depfile tracking)
# ─────────────────────────────────────────────────────────────

def test_shared_header_modification_triggers_rebuild(tmp_env):
    tmp_path, cache, graph, rebuilder = tmp_env

    header = tmp_path / "common.h"
    header.write_text("#define VALUE 10")
    src = tmp_path / "math.cpp"
    src.write_text('#include "common.h"\nint math() { return VALUE; }')
    obj = tmp_path / "math.o"
    obj.write_text("OBJ")

    # Generate a compiler depfile (.d)
    depfile = tmp_path / "math.o.d"
    depfile.write_text(f"math.o: math.cpp {header.name}\n")

    task = TaskNode(
        task_id="math.o",
        rule_name="cxx",
        outputs=["math.o"],
        explicit_inputs=["math.cpp"],
        variables={"DEP_FILE": "math.o.d"},
    )
    graph.add_task(task)
    rebuilder.record_success(task, graph)

    # Modify shared header common.h
    header.write_text("#define VALUE 20 /* CHANGED */")

    decision = rebuilder.evaluate_task(task, graph)
    assert decision.action == RebuildAction.BUILD
    assert decision.reason == RebuildReason.INPUT_CHANGED


# ─────────────────────────────────────────────────────────────
# 20. Real CMake manifest integration
# ─────────────────────────────────────────────────────────────

def test_real_cmake_manifest_rebuilder_integration(tmp_path):
    """
    Test rebuilder evaluation against the actual CMake-generated project.
    On first evaluation with no cache, all required tasks must require BUILD.
    """
    manifest_path = os.path.join(
        PROJECT_ROOT, "examples", "cpp_demo", "build", "build.ninja"
    )
    if not os.path.isfile(manifest_path):
        pytest.skip("CMake build.ninja not found.")

    parser = NinjaParser()
    manifest = parser.parse_file(manifest_path)
    base_dir = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "build")
    graph = BuildGraph.from_manifest(manifest, parser=parser, base_dir=base_dir)

    db_path = str(tmp_path / "real_manifest_cache.db")
    cache = BuildCache(db_path=db_path)
    rebuilder = Rebuilder(cache=cache, base_dir=base_dir)

    # First clean evaluation for target 'all'
    plan = rebuilder.evaluate_plan(graph, target="all")

    # All 9 tasks must be marked BUILD because there is no previous trace
    assert len(plan) == 9
    for tid, decision in plan.items():
        assert decision.action == RebuildAction.BUILD
        assert decision.reason in (RebuildReason.FIRST_BUILD, RebuildReason.DEPENDENCY_CHANGED)

    cache.close()
