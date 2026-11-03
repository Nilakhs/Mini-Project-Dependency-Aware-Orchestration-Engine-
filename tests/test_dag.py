"""
Tests for E1 — Build Graph Engine (DAG).

Validates:
1. Simple linear dependency chain.
2. Independent tasks.
3. Multiple independent compilation tasks.
4. Multiple outputs from one build edge.
5. Explicit dependency resolution.
6. Implicit dependency resolution.
7. Order-only dependency resolution.
8. Phony target resolution.
9. Default target resolution.
10. Explicit target selection.
11. Unknown target handling.
12. Simple cycle detection.
13. Complex cycle detection.
14. Disconnected graph components.
15. Duplicate dependency prevention.
16. Windows path normalization.
17. Dependency closure correctness.
18. Topological ordering correctness.
19. Parallelism opportunity identification.
20. Actual CMake-generated manifest integration.
"""

import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from engine.models import (
    BuildEdge,
    BuildRule,
    DependencyType,
    EdgeType,
    NinjaManifest,
    TaskNode,
)
from engine.ninja_parser import NinjaParser
from engine.dag import (
    BuildGraph,
    CycleError,
    GraphError,
    TargetNotFoundError,
    normalize_path,
    path_lookup_key,
)


# ─────────────────────────────────────────────────────────────
# 1. Simple linear dependency chain
# ─────────────────────────────────────────────────────────────

def test_linear_dependency_chain():
    """A -> B -> C: Task C depends on B, B depends on A."""
    graph = BuildGraph()
    task_a = TaskNode(task_id="A", rule_name="compile", outputs=["a.o"], explicit_inputs=["a.c"])
    task_b = TaskNode(task_id="B", rule_name="compile", outputs=["b.o"], explicit_inputs=["b.c"])
    task_c = TaskNode(task_id="C", rule_name="link", outputs=["c.exe"], explicit_inputs=["a.o", "b.o"])

    graph.add_task(task_a)
    graph.add_task(task_b)
    graph.add_task(task_c)

    graph.add_dependency("B", "A", DependencyType.EXPLICIT)
    graph.add_dependency("C", "B", DependencyType.EXPLICIT)

    order = [t.task_id for t in graph.topological_sort()]
    assert order.index("A") < order.index("B") < order.index("C")
    assert not graph.has_cycles()


# ─────────────────────────────────────────────────────────────
# 2. Independent tasks
# ─────────────────────────────────────────────────────────────

def test_independent_tasks():
    """Tasks with no prerequisites should all have in-degree 0."""
    graph = BuildGraph()
    for name in ["T1", "T2", "T3"]:
        graph.add_task(TaskNode(task_id=name, rule_name="task", outputs=[f"{name}.out"]))

    independent = graph.get_independent_tasks()
    assert len(independent) == 3
    assert {t.task_id for t in independent} == {"T1", "T2", "T3"}

    batches = graph.get_parallel_batches()
    assert len(batches) == 1
    assert len(batches[0]) == 3


# ─────────────────────────────────────────────────────────────
# 3. Multiple independent compilation tasks
# ─────────────────────────────────────────────────────────────

def test_multiple_independent_compilations():
    """Simulate 4 independent compilation tasks and 1 link task."""
    manifest = NinjaManifest()
    manifest.rules["cxx"] = BuildRule(name="cxx", variables={"command": "g++ -c $in -o $out"})
    manifest.rules["link"] = BuildRule(name="link", variables={"command": "g++ $in -o $out"})

    objs = []
    for src in ["math.cpp", "utils.cpp", "stringops.cpp", "fileio.cpp"]:
        obj = f"{src}.o"
        objs.append(obj)
        manifest.edges.append(
            BuildEdge(outputs=[obj], rule_name="cxx", explicit_inputs=[src], edge_type=EdgeType.BUILD)
        )

    manifest.edges.append(
        BuildEdge(outputs=["app.exe"], rule_name="link", explicit_inputs=objs, edge_type=EdgeType.BUILD)
    )

    graph = BuildGraph.from_manifest(manifest)
    batches = graph.get_parallel_batches()

    assert len(batches) == 2
    # Batch 0: all 4 compiles can run concurrently
    assert len(batches[0]) == 4
    # Batch 1: the link task
    assert len(batches[1]) == 1
    assert batches[1][0].outputs == ["app.exe"]


# ─────────────────────────────────────────────────────────────
# 4. Multiple outputs from one build edge
# ─────────────────────────────────────────────────────────────

def test_multiple_outputs_share_one_producer():
    """When a task generates both a header and source (e.g. bison), both map to it."""
    graph = BuildGraph()
    task = TaskNode(
        task_id="bison_gen",
        rule_name="bison",
        outputs=["parser.cpp", "parser.h"],
        explicit_inputs=["parser.y"],
    )
    graph.add_task(task)

    assert graph.producers[path_lookup_key("parser.cpp")] == "bison_gen"
    assert graph.producers[path_lookup_key("parser.h")] == "bison_gen"


# ─────────────────────────────────────────────────────────────
# 5. Explicit dependency resolution
# ─────────────────────────────────────────────────────────────

def test_explicit_dependency_resolution():
    """Explicit inputs before '|' are classified as DependencyType.EXPLICIT."""
    manifest = NinjaManifest()
    manifest.edges.append(
        BuildEdge(outputs=["foo.o"], rule_name="cxx", explicit_inputs=["foo.cpp"], edge_type=EdgeType.BUILD)
    )
    manifest.edges.append(
        BuildEdge(outputs=["foo.exe"], rule_name="link", explicit_inputs=["foo.o"], edge_type=EdgeType.BUILD)
    )

    graph = BuildGraph.from_manifest(manifest)
    link_task = graph.nodes["foo.exe"]

    assert "foo.o" in link_task.dependencies
    assert link_task.dependencies["foo.o"] == DependencyType.EXPLICIT


# ─────────────────────────────────────────────────────────────
# 6. Implicit dependency resolution
# ─────────────────────────────────────────────────────────────

def test_implicit_dependency_resolution():
    """Implicit inputs between '|' and '||' are classified as DependencyType.IMPLICIT."""
    manifest = NinjaManifest()
    manifest.edges.append(
        BuildEdge(outputs=["libfoo.a"], rule_name="ar", explicit_inputs=["foo.o"], edge_type=EdgeType.BUILD)
    )
    manifest.edges.append(
        BuildEdge(
            outputs=["app.exe"],
            rule_name="link",
            explicit_inputs=["main.o"],
            implicit_inputs=["libfoo.a"],
            edge_type=EdgeType.BUILD,
        )
    )

    graph = BuildGraph.from_manifest(manifest)
    app_task = graph.nodes["app.exe"]

    assert "libfoo.a" in app_task.dependencies
    assert app_task.dependencies["libfoo.a"] == DependencyType.IMPLICIT
    assert "libfoo.a" in app_task.rebuild_dependencies


# ─────────────────────────────────────────────────────────────
# 7. Order-only dependency resolution
# ─────────────────────────────────────────────────────────────

def test_order_only_dependency_resolution():
    """Order-only inputs after '||' enforce ordering but do not trigger rebuilds."""
    manifest = NinjaManifest()
    manifest.edges.append(
        BuildEdge(outputs=["gen_headers"], rule_name="codegen", explicit_inputs=["spec.txt"], edge_type=EdgeType.BUILD)
    )
    manifest.edges.append(
        BuildEdge(
            outputs=["main.o"],
            rule_name="cxx",
            explicit_inputs=["main.cpp"],
            order_only_inputs=["gen_headers"],
            edge_type=EdgeType.BUILD,
        )
    )

    graph = BuildGraph.from_manifest(manifest)
    main_task = graph.nodes["main.o"]

    assert "gen_headers" in main_task.dependencies
    assert main_task.dependencies["gen_headers"] == DependencyType.ORDER_ONLY
    assert "gen_headers" in main_task.order_only_dependencies
    assert "gen_headers" not in main_task.rebuild_dependencies


# ─────────────────────────────────────────────────────────────
# 8. Phony target resolution
# ─────────────────────────────────────────────────────────────

def test_phony_target_resolution():
    """Phony aliases should not become tasks, and should resolve to real tasks."""
    manifest = NinjaManifest()
    manifest.edges.append(
        BuildEdge(outputs=["app.exe"], rule_name="link", explicit_inputs=["app.o"], edge_type=EdgeType.BUILD)
    )
    # Phony alias
    manifest.edges.append(
        BuildEdge(outputs=["app"], rule_name="phony", explicit_inputs=["app.exe"], edge_type=EdgeType.PHONY)
    )

    graph = BuildGraph.from_manifest(manifest)

    # Phony target is NOT a TaskNode
    assert "app" not in graph.nodes
    assert "app.exe" in graph.nodes

    # Resolving 'app' yields ['app.exe']
    resolved = graph.resolve_target("app")
    assert resolved == ["app.exe"]


# ─────────────────────────────────────────────────────────────
# 9. Default target resolution
# ─────────────────────────────────────────────────────────────

def test_default_target_resolution():
    """The default target from manifest should resolve properly."""
    manifest = NinjaManifest()
    manifest.defaults = ["all"]
    manifest.edges.append(
        BuildEdge(outputs=["t1.exe"], rule_name="link", explicit_inputs=["t1.o"], edge_type=EdgeType.BUILD)
    )
    manifest.edges.append(
        BuildEdge(outputs=["all"], rule_name="phony", explicit_inputs=["t1.exe"], edge_type=EdgeType.PHONY)
    )

    graph = BuildGraph.from_manifest(manifest)
    closure = graph.get_target_closure()  # None -> uses defaults
    assert "t1.exe" in closure


# ─────────────────────────────────────────────────────────────
# 10. Explicit target selection
# ─────────────────────────────────────────────────────────────

def test_explicit_target_selection_isolates_dependencies():
    """Selecting target 'demo.exe' does not build unrelated 'test_runner.exe'."""
    manifest = NinjaManifest()
    # Library
    manifest.edges.append(BuildEdge(outputs=["lib.a"], rule_name="ar", explicit_inputs=["lib.o"], edge_type=EdgeType.BUILD))
    # Target 1: demo
    manifest.edges.append(
        BuildEdge(outputs=["demo.exe"], rule_name="link", explicit_inputs=["demo.o"], implicit_inputs=["lib.a"], edge_type=EdgeType.BUILD)
    )
    # Target 2: test_runner
    manifest.edges.append(
        BuildEdge(outputs=["test_runner.exe"], rule_name="link", explicit_inputs=["tr.o"], implicit_inputs=["lib.a"], edge_type=EdgeType.BUILD)
    )

    graph = BuildGraph.from_manifest(manifest)

    demo_closure = graph.get_target_closure("demo.exe")
    assert "demo.exe" in demo_closure
    assert "lib.a" in demo_closure
    assert "test_runner.exe" not in demo_closure


# ─────────────────────────────────────────────────────────────
# 11. Unknown target handling
# ─────────────────────────────────────────────────────────────

def test_unknown_target_raises_error():
    """Requesting a nonexistent target should raise TargetNotFoundError."""
    graph = BuildGraph()
    graph.add_task(TaskNode(task_id="A", rule_name="cxx", outputs=["a.o"]))

    with pytest.raises(TargetNotFoundError) as exc_info:
        graph.resolve_target("nonexistent_target")
    assert "not found" in str(exc_info.value).lower()


# ─────────────────────────────────────────────────────────────
# 12. Simple cycle detection
# ─────────────────────────────────────────────────────────────

def test_simple_cycle_detection():
    """A -> B -> A should be detected with exact cycle path."""
    graph = BuildGraph()
    graph.add_task(TaskNode(task_id="A", rule_name="r1", outputs=["a.out"]))
    graph.add_task(TaskNode(task_id="B", rule_name="r2", outputs=["b.out"]))

    graph.add_dependency("A", "B")
    graph.add_dependency("B", "A")

    assert graph.has_cycles()
    with pytest.raises(CycleError) as exc_info:
        graph.detect_cycles()

    err = str(exc_info.value)
    assert "A" in err and "B" in err
    assert "->" in err


# ─────────────────────────────────────────────────────────────
# 13. Complex cycle detection
# ─────────────────────────────────────────────────────────────

def test_complex_cycle_detection():
    """A -> B -> C -> D -> B should isolate the cycle path."""
    graph = BuildGraph()
    for name in ["A", "B", "C", "D", "E"]:
        graph.add_task(TaskNode(task_id=name, rule_name="r", outputs=[f"{name}.out"]))

    graph.add_dependency("A", "B")
    graph.add_dependency("B", "C")
    graph.add_dependency("C", "D")
    graph.add_dependency("D", "B")  # Cycle B -> C -> D -> B
    graph.add_dependency("E", "A")

    assert graph.has_cycles()
    with pytest.raises(CycleError) as exc_info:
        graph.detect_cycles()

    cycle = exc_info.value.cycle
    assert "B" in cycle and "C" in cycle and "D" in cycle


# ─────────────────────────────────────────────────────────────
# 14. Disconnected graph components
# ─────────────────────────────────────────────────────────────

def test_disconnected_graph_components():
    """Graph with two completely separate subgraphs."""
    graph = BuildGraph()
    # Subgraph 1: A -> B
    graph.add_task(TaskNode(task_id="A", rule_name="r", outputs=["a.out"]))
    graph.add_task(TaskNode(task_id="B", rule_name="r", outputs=["b.out"]))
    graph.add_dependency("B", "A")

    # Subgraph 2: X -> Y
    graph.add_task(TaskNode(task_id="X", rule_name="r", outputs=["x.out"]))
    graph.add_task(TaskNode(task_id="Y", rule_name="r", outputs=["y.out"]))
    graph.add_dependency("Y", "X")

    order = [t.task_id for t in graph.topological_sort()]
    assert len(order) == 4
    assert order.index("A") < order.index("B")
    assert order.index("X") < order.index("Y")


# ─────────────────────────────────────────────────────────────
# 15. Duplicate dependency prevention
# ─────────────────────────────────────────────────────────────

def test_duplicate_dependency_prevention():
    """Adding multiple dependencies between same tasks upgrades rather than duplicates."""
    graph = BuildGraph()
    graph.add_task(TaskNode(task_id="A", rule_name="r", outputs=["a.out"]))
    graph.add_task(TaskNode(task_id="B", rule_name="r", outputs=["b.out"]))

    # Add as ORDER_ONLY first, then EXPLICIT
    graph.add_dependency("B", "A", DependencyType.ORDER_ONLY)
    assert graph.nodes["B"].dependencies["A"] == DependencyType.ORDER_ONLY

    # Upgrade to EXPLICIT
    graph.add_dependency("B", "A", DependencyType.EXPLICIT)
    assert graph.nodes["B"].dependencies["A"] == DependencyType.EXPLICIT
    assert len(graph.nodes["B"].dependencies) == 1
    assert len(graph.nodes["A"].dependents) == 1


# ─────────────────────────────────────────────────────────────
# 16. Windows path normalization
# ─────────────────────────────────────────────────────────────

def test_windows_path_normalization():
    """Backslashes and case differences should normalize to identical graph lookups."""
    path1 = "CMakeFiles\\mylib.dir\\src\\math.cpp.obj"
    path2 = "CMakeFiles/mylib.dir/src/math.cpp.obj"

    assert normalize_path(path1) == normalize_path(path2)
    assert path_lookup_key(path1) == path_lookup_key(path2)

    manifest = NinjaManifest()
    manifest.edges.append(
        BuildEdge(outputs=[path1], rule_name="cxx", explicit_inputs=["src/math.cpp"], edge_type=EdgeType.BUILD)
    )
    manifest.edges.append(
        BuildEdge(outputs=["lib.a"], rule_name="ar", explicit_inputs=[path2], edge_type=EdgeType.BUILD)
    )

    graph = BuildGraph.from_manifest(manifest)
    lib_task = graph.nodes["lib.a"]

    # lib.a must find its prerequisite despite slash difference
    assert len(lib_task.dependencies) == 1


# ─────────────────────────────────────────────────────────────
# 17. Dependency closure correctness
# ─────────────────────────────────────────────────────────────

def test_dependency_closure_no_duplicates():
    """Diamond dependency: Top -> Left, Right -> Bottom."""
    graph = BuildGraph()
    graph.add_task(TaskNode(task_id="Bottom", rule_name="r", outputs=["bottom.out"]))
    graph.add_task(TaskNode(task_id="Left", rule_name="r", outputs=["left.out"]))
    graph.add_task(TaskNode(task_id="Right", rule_name="r", outputs=["right.out"]))
    graph.add_task(TaskNode(task_id="Top", rule_name="r", outputs=["top.out"]))

    graph.add_dependency("Left", "Bottom")
    graph.add_dependency("Right", "Bottom")
    graph.add_dependency("Top", "Left")
    graph.add_dependency("Top", "Right")

    closure = graph.get_target_closure("Top")
    assert closure == {"Top", "Left", "Right", "Bottom"}
    assert len(closure) == 4


# ─────────────────────────────────────────────────────────────
# 18. Topological ordering correctness
# ─────────────────────────────────────────────────────────────

def test_topological_ordering_respects_all_edges():
    """In topological order, every prerequisite must appear before its dependent."""
    graph = BuildGraph()
    for name in ["A", "B", "C", "D"]:
        graph.add_task(TaskNode(task_id=name, rule_name="r", outputs=[f"{name}.out"]))

    graph.add_dependency("B", "A")
    graph.add_dependency("C", "A")
    graph.add_dependency("D", "B")
    graph.add_dependency("D", "C")

    ordered = [t.task_id for t in graph.topological_sort()]
    assert ordered.index("A") < ordered.index("B")
    assert ordered.index("A") < ordered.index("C")
    assert ordered.index("B") < ordered.index("D")
    assert ordered.index("C") < ordered.index("D")


# ─────────────────────────────────────────────────────────────
# 19. Parallelism opportunity identification
# ─────────────────────────────────────────────────────────────

def test_parallelism_opportunity_waves():
    """Verify that tasks at the same topological wave are identified as parallelizable."""
    graph = BuildGraph()
    for i in range(4):
        graph.add_task(TaskNode(task_id=f"compile_{i}", rule_name="cxx", outputs=[f"{i}.o"]))

    graph.add_task(TaskNode(task_id="link", rule_name="ld", outputs=["app.exe"]))
    for i in range(4):
        graph.add_dependency("link", f"compile_{i}")

    batches = graph.get_parallel_batches()
    assert len(batches) == 2
    assert len(batches[0]) == 4  # 4 compiles run concurrently
    assert len(batches[1]) == 1  # link runs after all 4 finish


# ─────────────────────────────────────────────────────────────
# 20. Actual CMake-generated manifest integration
# ─────────────────────────────────────────────────────────────

def test_real_cmake_manifest_dag_integration():
    """
    Integration test using the real CMake-generated build.ninja:
    - 4 independent mylib compilations
    - static library depends on all 4 object files
    - demo.exe depends on its object and the library
    - test_runner.exe depends on its object and the library
    - demo.exe and test_runner.exe can link independently
    - target 'all' resolves to complete build
    - phony aliases do not become executable tasks
    """
    manifest_path = os.path.join(
        PROJECT_ROOT, "examples", "cpp_demo", "build", "build.ninja"
    )
    if not os.path.isfile(manifest_path):
        pytest.skip("CMake build.ninja not found.")

    parser = NinjaParser()
    manifest = parser.parse_file(manifest_path)
    graph = BuildGraph.from_manifest(manifest, parser=parser)

    # 1. Total executable tasks: 9
    # (4 mylib compiles + 1 libmylib.a + 1 demo compile + 1 demo.exe + 1 test_runner compile + 1 test_runner.exe)
    assert len(graph.nodes) >= 9

    # 2. Phony aliases are NOT in graph.nodes
    for phony_edge in manifest.get_phony_edges():
        for out in phony_edge.outputs:
            assert out not in graph.nodes, f"Phony target '{out}' should not be a TaskNode"

    # 3. Target 'all' resolves to complete build
    all_closure = graph.get_target_closure("all")
    assert "demo.exe" in all_closure
    assert "test_runner.exe" in all_closure
    assert "libmylib.a" in all_closure

    # 4. Check static library dependencies
    lib_task = graph.nodes["libmylib.a"]
    assert len(lib_task.dependencies) == 4
    for obj in [
        "CMakeFiles/mylib.dir/src/math.cpp.obj",
        "CMakeFiles/mylib.dir/src/utils.cpp.obj",
        "CMakeFiles/mylib.dir/src/stringops.cpp.obj",
        "CMakeFiles/mylib.dir/src/fileio.cpp.obj",
    ]:
        assert obj in lib_task.dependencies

    # 5. Check demo.exe dependencies
    demo_task = graph.nodes["demo.exe"]
    assert "CMakeFiles/demo.dir/src/main.cpp.obj" in demo_task.dependencies
    assert "libmylib.a" in demo_task.dependencies

    # 6. Check test_runner.exe dependencies
    tr_task = graph.nodes["test_runner.exe"]
    assert "CMakeFiles/test_runner.dir/src/test_runner.cpp.obj" in tr_task.dependencies
    assert "libmylib.a" in tr_task.dependencies

    # 7. Check parallel waves
    batches = graph.get_parallel_batches(subset=all_closure)
    assert len(batches) >= 3

    # Wave 0 must contain all compilation tasks (at least 6 independent compiles)
    wave_0_tasks = {t.primary_output for t in batches[0]}
    assert "CMakeFiles/mylib.dir/src/math.cpp.obj" in wave_0_tasks
    assert "CMakeFiles/mylib.dir/src/utils.cpp.obj" in wave_0_tasks
    assert "CMakeFiles/mylib.dir/src/stringops.cpp.obj" in wave_0_tasks
    assert "CMakeFiles/mylib.dir/src/fileio.cpp.obj" in wave_0_tasks
    assert "CMakeFiles/demo.dir/src/main.cpp.obj" in wave_0_tasks
    assert "CMakeFiles/test_runner.dir/src/test_runner.cpp.obj" in wave_0_tasks

    # No cycles
    assert not graph.has_cycles()

    # Generate summary & mermaid string
    summary = graph.summary()
    assert "Build Dependency Graph Summary (DAG)" in summary
    mermaid = graph.to_mermaid("all")
    assert "graph TD" in mermaid
    assert "libmylib.a" in mermaid
