"""
E1 — Build Graph Engine (DAG)

Constructs and manages the Directed Acyclic Graph (DAG) of build tasks.
Consumes the parsed NinjaManifest from Phase 1 and provides:
  - Adjacency-list task graph (TaskNode)
  - Producer mapping (output_file -> producing TaskNode)
  - Explicit, Implicit, and Order-Only dependency tracking
  - Phony target resolution (aliases resolved down to real tasks)
  - Three-colour DFS cycle detection (WHITE / GRAY / BLACK) with exact cycle path reporting
  - Topological ordering via Kahn's algorithm (full graph and target-specific)
  - Target selection and dependency closure calculation
  - Parallelism opportunity identification (independent tasks & wave levels)
  - Mermaid diagram generation for visualization
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Union

from engine.models import (
    BuildEdge,
    BuildRule,
    DependencyType,
    EdgeType,
    NinjaManifest,
    TaskNode,
    UNSUPPORTED_RULES,
)


# ─────────────────────────────────────────────────────────────
# Path Normalization Utilities
# ─────────────────────────────────────────────────────────────

def normalize_path(path: str) -> str:
    """
    Normalize file paths for consistent graph mapping across operating systems.
    - Standardizes backslashes to forward slashes.
    - Collapses redundant separators, '.' and '..'.
    - Strips trailing slashes (unless root).
    """
    if not path:
        return ""
    p = path.replace("\\", "/")
    p = os.path.normpath(p).replace("\\", "/")
    return p


def path_lookup_key(path: str) -> str:
    """
    Generate a case-normalized lookup key for dictionary indexes.
    On Windows, file systems are case-insensitive, so paths are lowercased.
    """
    p = normalize_path(path)
    if os.name == "nt" or sys.platform == "win32":
        return p.lower()
    return p


# ─────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────

class GraphError(Exception):
    """Base exception for build graph operations."""
    pass


class CycleError(GraphError):
    """Raised when a dependency cycle is detected in the graph."""

    def __init__(self, message: str, cycle: Optional[list[str]] = None):
        super().__init__(message)
        self.cycle = cycle or []


class TargetNotFoundError(GraphError):
    """Raised when a requested target does not exist in the graph."""
    pass


class DependencyResolutionError(GraphError):
    """Raised when a dependency cannot be resolved."""
    pass


# ─────────────────────────────────────────────────────────────
# DFS Colors for Cycle Detection
# ─────────────────────────────────────────────────────────────

WHITE = 0  # Unvisited
GRAY = 1   # Currently in recursion stack (being explored)
BLACK = 2  # Fully explored


# ─────────────────────────────────────────────────────────────
# Build Graph (DAG)
# ─────────────────────────────────────────────────────────────

class BuildGraph:
    """
    Directed Acyclic Graph (DAG) of executable build tasks.

    Adjacency-list representation:
    - nodes: task_id -> TaskNode
    - producers: path_lookup_key(output) -> task_id
    - phony_targets: path_lookup_key(alias) -> list of (input_path, DependencyType)
    - source_files: set of normalized paths for source inputs (no producing task)
    """

    def __init__(self, base_dir: str = ""):
        self.nodes: dict[str, TaskNode] = {}
        # output lookup key -> task_id
        self.producers: dict[str, str] = {}
        # output lookup key -> normalized display path
        self.producers_display: dict[str, str] = {}
        # phony target lookup key -> list of (input_path, DependencyType)
        self.phony_targets: dict[str, list[tuple[str, DependencyType]]] = {}
        # phony lookup key -> normalized display name
        self.phony_display: dict[str, str] = {}
        # Default targets from manifest (e.g. ['all'])
        self.defaults: list[str] = []
        # External source files (inputs not produced by any task)
        self.source_files: set[str] = set()
        # Diagnostic warnings
        self.warnings: list[str] = []
        # Base directory for relative file checks
        self.base_dir: str = base_dir
        # Reference to manifest if built from one
        self.manifest: Optional[NinjaManifest] = None

    # ─────────────────────────────────────────────────────────
    # Task Management
    # ─────────────────────────────────────────────────────────

    def add_task(self, task: TaskNode) -> None:
        """
        Add a TaskNode to the graph and register all its outputs in the producer map.
        Raises GraphError if duplicate task_id is used.
        """
        if task.task_id in self.nodes:
            raise GraphError(f"Duplicate task ID in build graph: '{task.task_id}'")

        self.nodes[task.task_id] = task

        # Map all outputs (explicit and implicit) to this task
        for out in task.all_outputs:
            key = path_lookup_key(out)
            if key in self.producers and self.producers[key] != task.task_id:
                existing_tid = self.producers[key]
                self.warnings.append(
                    f"Multiple tasks produce output '{out}': {existing_tid} and {task.task_id}"
                )
            self.producers[key] = task.task_id
            self.producers_display[key] = normalize_path(out)

    def add_dependency(
        self,
        dependent_id: str,
        prerequisite_id: str,
        dep_type: DependencyType = DependencyType.EXPLICIT,
    ) -> None:
        """
        Add a directed dependency edge: dependent_id requires prerequisite_id.
        Merges dependency types if an edge already exists, prioritizing EXPLICIT > IMPLICIT > ORDER_ONLY.
        """
        if dependent_id not in self.nodes:
            raise GraphError(f"Dependent task '{dependent_id}' not found in graph.")
        if prerequisite_id not in self.nodes:
            raise GraphError(f"Prerequisite task '{prerequisite_id}' not found in graph.")

        dept_node = self.nodes[dependent_id]
        prereq_node = self.nodes[prerequisite_id]

        if prerequisite_id in dept_node.dependencies:
            dept_node.dependencies[prerequisite_id] = DependencyType.merge(
                dept_node.dependencies[prerequisite_id], dep_type
            )
        else:
            dept_node.dependencies[prerequisite_id] = dep_type

        prereq_node.dependents.add(dependent_id)

    # ─────────────────────────────────────────────────────────
    # Manifest Ingestion
    # ─────────────────────────────────────────────────────────

    @classmethod
    def from_manifest(
        cls,
        manifest: NinjaManifest,
        parser: Optional[object] = None,
        base_dir: str = "",
    ) -> "BuildGraph":
        """
        Construct a BuildGraph from a parsed NinjaManifest.

        1. Ingests phony targets as dependency aliases.
        2. Creates TaskNodes for all executable build edges.
        3. Maps all outputs to producing tasks.
        4. Resolves explicit, implicit, and order-only dependencies.
        5. Identifies external source inputs.
        """
        from engine.ninja_parser import NinjaParser

        graph = cls(base_dir=base_dir)
        graph.manifest = manifest
        graph.defaults = list(manifest.defaults)

        if parser is None:
            parser = NinjaParser()
            parser._manifest = manifest

        # ── Step 1: Register phony targets (aliases) ──
        for edge in manifest.get_phony_edges():
            for out in edge.outputs:
                key = path_lookup_key(out)
                inputs: list[tuple[str, DependencyType]] = []
                for inp in edge.explicit_inputs:
                    inputs.append((inp, DependencyType.EXPLICIT))
                for inp in edge.implicit_inputs:
                    inputs.append((inp, DependencyType.IMPLICIT))
                for inp in edge.order_only_inputs:
                    inputs.append((inp, DependencyType.ORDER_ONLY))

                graph.phony_targets[key] = inputs
                graph.phony_display[key] = normalize_path(out)

        # ── Step 2: Create TaskNodes for executable edges ──
        executable_edges: list[BuildEdge] = []
        for edge in manifest.edges:
            if edge.edge_type in (EdgeType.BUILD, EdgeType.CUSTOM_COMMAND):
                executable_edges.append(edge)

        task_edge_map: dict[str, BuildEdge] = {}

        for idx, edge in enumerate(executable_edges):
            # Deterministic, unique task_id based on primary output
            primary = normalize_path(edge.primary_output) if edge.primary_output else f"task_{idx}"
            task_id = primary
            if task_id in graph.nodes:
                task_id = f"{primary}#{idx}"

            # Resolve command and description using parser
            cmd = parser.resolve_edge_command(edge) if hasattr(parser, "resolve_edge_command") else ""
            desc = parser.resolve_edge_description(edge) if hasattr(parser, "resolve_edge_description") else ""

            # Look up rule variables
            rule = manifest.get_rule(edge.rule_name)
            rule_vars = dict(rule.variables) if rule else {}

            task = TaskNode(
                task_id=task_id,
                rule_name=edge.rule_name,
                command=cmd,
                description=desc,
                outputs=[normalize_path(o) for o in edge.outputs],
                implicit_outputs=[normalize_path(o) for o in edge.implicit_outputs],
                explicit_inputs=[normalize_path(i) for i in edge.explicit_inputs],
                implicit_inputs=[normalize_path(i) for i in edge.implicit_inputs],
                order_only_inputs=[normalize_path(i) for i in edge.order_only_inputs],
                variables=dict(edge.variables),
                rule_variables=rule_vars,
            )

            graph.add_task(task)
            task_edge_map[task_id] = edge

        # ── Step 3: Resolve dependencies for each task ──
        for task_id, task in graph.nodes.items():
            edge = task_edge_map[task_id]

            # Collect all inputs with their category
            all_input_specs: list[tuple[str, DependencyType]] = []
            for inp in edge.explicit_inputs:
                all_input_specs.append((inp, DependencyType.EXPLICIT))
            for inp in edge.implicit_inputs:
                all_input_specs.append((inp, DependencyType.IMPLICIT))
            for inp in edge.order_only_inputs:
                all_input_specs.append((inp, DependencyType.ORDER_ONLY))

            for inp_path, dep_type in all_input_specs:
                key = path_lookup_key(inp_path)

                if key in graph.producers:
                    prereq_id = graph.producers[key]
                    if prereq_id != task_id:
                        graph.add_dependency(task_id, prereq_id, dep_type)
                elif key in graph.phony_targets:
                    # Expand phony target aliases
                    expanded = graph._expand_phony_target(key, dep_type)
                    for prereq_id, effective_type in expanded:
                        if prereq_id != task_id and prereq_id in graph.nodes:
                            graph.add_dependency(task_id, prereq_id, effective_type)
                else:
                    # External source file input
                    norm_inp = normalize_path(inp_path)
                    task.source_inputs.append((norm_inp, dep_type))
                    graph.source_files.add(norm_inp)

        return graph

    def _expand_phony_target(
        self,
        phony_key: str,
        parent_dep_type: DependencyType,
        visited: Optional[set[str]] = None,
    ) -> list[tuple[str, DependencyType]]:
        """
        Recursively expand a phony target down to concrete producing task IDs.
        Preserves dependency semantics (ORDER_ONLY stays ORDER_ONLY).
        """
        if visited is None:
            visited = set()
        if phony_key in visited:
            return []
        visited.add(phony_key)

        results: list[tuple[str, DependencyType]] = []
        for sub_input, sub_dep_type in self.phony_targets.get(phony_key, []):
            sub_key = path_lookup_key(sub_input)
            # If the parent edge specified order-only, order-only semantics apply to all expanded deps
            effective_type = (
                DependencyType.ORDER_ONLY
                if parent_dep_type == DependencyType.ORDER_ONLY
                else sub_dep_type
            )

            if sub_key in self.producers:
                results.append((self.producers[sub_key], effective_type))
            elif sub_key in self.phony_targets:
                results.extend(
                    self._expand_phony_target(sub_key, effective_type, visited)
                )
            else:
                # Source file or external path referenced through phony
                norm_p = normalize_path(sub_input)
                self.source_files.add(norm_p)

        return results

    # ─────────────────────────────────────────────────────────
    # Target Resolution & Dependency Closure
    # ─────────────────────────────────────────────────────────

    def resolve_target(self, target: str) -> list[str]:
        """
        Resolve a target name (output file, phony target, or task_id)
        to a list of concrete task IDs.
        Raises TargetNotFoundError if target is not known.
        """
        if not target:
            raise TargetNotFoundError("Target name cannot be empty.")

        # 1. Direct task_id match
        if target in self.nodes:
            return [target]

        key = path_lookup_key(target)

        # 2. Output file in producers
        if key in self.producers:
            return [self.producers[key]]

        # 3. Phony alias
        if key in self.phony_targets:
            expanded = self._expand_phony_target(key, DependencyType.EXPLICIT)
            task_ids = list(dict.fromkeys(tid for tid, _ in expanded))
            if task_ids:
                return task_ids
            # Phony target exists but produces no build tasks (e.g. order-only dummy)
            return []

        # 4. Normalized path match across any output
        norm_t = normalize_path(target)
        for tid, node in self.nodes.items():
            if norm_t in [normalize_path(o) for o in node.all_outputs]:
                return [tid]

        raise TargetNotFoundError(f"Target '{target}' not found in build graph.")

    def get_target_closure(self, target: Optional[Union[str, list[str]]] = None) -> set[str]:
        """
        Compute the upstream dependency closure for the requested target(s).
        If target is None or empty, resolves manifest defaults (or all tasks).
        Returns the set of task IDs required to build the target(s).
        No task appears twice.
        """
        if target is None:
            if self.defaults:
                targets = list(self.defaults)
            else:
                # Default to all tasks if no default statement
                return set(self.nodes.keys())
        elif isinstance(target, str):
            targets = [target]
        else:
            targets = list(target)

        # Resolve all targets to root task IDs
        root_task_ids: set[str] = set()
        for t in targets:
            resolved = self.resolve_target(t)
            root_task_ids.update(resolved)

        # Upstream traversal
        closure: set[str] = set()
        stack = list(root_task_ids)

        while stack:
            tid = stack.pop()
            if tid not in closure and tid in self.nodes:
                closure.add(tid)
                for prereq_id in self.nodes[tid].dependencies:
                    if prereq_id not in closure:
                        stack.append(prereq_id)

        return closure

    # ─────────────────────────────────────────────────────────
    # Three-Colour DFS Cycle Detection
    # ─────────────────────────────────────────────────────────

    def detect_cycles(self) -> list[str]:
        """
        Detect dependency cycles using Three-Colour DFS (WHITE / GRAY / BLACK).
        Handles disconnected components.
        If a cycle is detected, raises CycleError with the exact cycle path:
            e.g., 'A -> B -> C -> A'.
        Returns empty list if the graph is strictly acyclic.
        """
        color: dict[str, int] = {tid: WHITE for tid in self.nodes}
        path: list[str] = []

        def dfs(node_id: str) -> None:
            color[node_id] = GRAY
            path.append(node_id)

            # Node depends on its prerequisites (prereqs must run first)
            for prereq_id in sorted(self.nodes[node_id].dependencies.keys()):
                if prereq_id not in self.nodes:
                    continue
                prereq_color = color.get(prereq_id, WHITE)
                if prereq_color == GRAY:
                    # Back-edge found -> cycle!
                    cycle_start = path.index(prereq_id)
                    cycle_path = path[cycle_start:] + [prereq_id]
                    cycle_str = " -> ".join(cycle_path)
                    raise CycleError(
                        f"Dependency cycle detected: {cycle_str}",
                        cycle=cycle_path,
                    )
                elif prereq_color == WHITE:
                    dfs(prereq_id)

            path.pop()
            color[node_id] = BLACK

        for tid in sorted(self.nodes.keys()):
            if color[tid] == WHITE:
                dfs(tid)

        return []

    def has_cycles(self) -> bool:
        """Return True if the graph contains any dependency cycles, False otherwise."""
        try:
            self.detect_cycles()
            return False
        except CycleError:
            return True

    # ─────────────────────────────────────────────────────────
    # Topological Ordering (Kahn's Algorithm)
    # ─────────────────────────────────────────────────────────

    def topological_sort(self, subset: Optional[set[str]] = None) -> list[TaskNode]:
        """
        Compute a topological ordering of tasks using Kahn's algorithm.
        Prerequisite tasks always appear before dependent tasks.
        Respects explicit, implicit, and order-only dependencies.

        Args:
            subset: If provided, orders only tasks within this set. Otherwise orders all nodes.

        Returns:
            List of TaskNodes in executable topological order.

        Raises:
            CycleError if a dependency cycle is encountered.
        """
        # Run cycle check first for clean diagnostics
        self.detect_cycles()

        nodes_to_order = subset if subset is not None else set(self.nodes.keys())
        if not nodes_to_order:
            return []

        # Calculate in-degree: number of prerequisites within nodes_to_order
        in_degree: dict[str, int] = {tid: 0 for tid in nodes_to_order}
        for tid in nodes_to_order:
            node = self.nodes[tid]
            prereqs = [dep for dep in node.dependencies if dep in nodes_to_order]
            in_degree[tid] = len(prereqs)

        # Ready queue: nodes with in-degree 0 (prerequisites satisfied)
        # Sorted queue ensures deterministic, reproducible ordering
        queue = [tid for tid in nodes_to_order if in_degree[tid] == 0]
        queue.sort()

        ordered_ids: list[str] = []
        while queue:
            tid = queue.pop(0)
            ordered_ids.append(tid)

            for dep_tid in sorted(self.nodes[tid].dependents):
                if dep_tid in nodes_to_order:
                    in_degree[dep_tid] -= 1
                    if in_degree[dep_tid] == 0:
                        queue.append(dep_tid)
                        queue.sort()

        if len(ordered_ids) != len(nodes_to_order):
            remaining = nodes_to_order - set(ordered_ids)
            raise CycleError(
                f"Dependency cycle detected among tasks: {sorted(remaining)}",
                cycle=sorted(remaining),
            )

        return [self.nodes[tid] for tid in ordered_ids]

    def topological_sort_for_target(
        self, target: Optional[Union[str, list[str]]] = None
    ) -> list[TaskNode]:
        """
        Compute topological ordering restricted to the dependency closure of the target(s).
        """
        closure = self.get_target_closure(target)
        return self.topological_sort(subset=closure)

    # ─────────────────────────────────────────────────────────
    # Parallelism Identification
    # ─────────────────────────────────────────────────────────

    def get_independent_tasks(
        self,
        completed: Optional[set[str]] = None,
        subset: Optional[set[str]] = None,
    ) -> list[TaskNode]:
        """
        Identify tasks whose prerequisites are completely satisfied.
        - If completed is None: returns tasks with 0 prerequisites in the scope.
        - If completed is provided: returns uncompleted tasks whose prerequisites are in completed.
        """
        scope = subset if subset is not None else set(self.nodes.keys())
        done = completed if completed is not None else set()

        ready: list[TaskNode] = []
        for tid in sorted(scope):
            if tid in done:
                continue
            node = self.nodes[tid]
            # Prereqs within scope
            prereqs = set(node.dependencies.keys())
            if subset is not None:
                prereqs = prereqs & subset
            if prereqs.issubset(done):
                ready.append(node)
        return ready

    def get_parallel_batches(
        self, subset: Optional[set[str]] = None
    ) -> list[list[TaskNode]]:
        """
        Partition tasks into execution levels (waves).
        All tasks within a wave are mutually independent and can execute concurrently.
        """
        self.detect_cycles()
        remaining = set(subset if subset is not None else self.nodes.keys())
        completed: set[str] = set()
        batches: list[list[TaskNode]] = []

        while remaining:
            batch = [
                node
                for node in self.get_independent_tasks(completed, subset=remaining)
                if node.task_id in remaining
            ]
            if not batch:
                raise CycleError("Cycle detected during parallel batch partitioning.")
            batches.append(batch)
            for node in batch:
                completed.add(node.task_id)
                remaining.remove(node.task_id)

        return batches

    # ─────────────────────────────────────────────────────────
    # Graph Diagnostics & Summaries
    # ─────────────────────────────────────────────────────────

    def summary(self) -> str:
        """Produce a comprehensive, human-readable summary of the build graph."""
        total_tasks = len(self.nodes)
        total_deps = sum(len(n.dependencies) for n in self.nodes.values())
        explicit_deps = sum(
            sum(1 for d in n.dependencies.values() if d == DependencyType.EXPLICIT)
            for n in self.nodes.values()
        )
        implicit_deps = sum(
            sum(1 for d in n.dependencies.values() if d == DependencyType.IMPLICIT)
            for n in self.nodes.values()
        )
        order_only_deps = sum(
            sum(1 for d in n.dependencies.values() if d == DependencyType.ORDER_ONLY)
            for n in self.nodes.values()
        )

        batches = self.get_parallel_batches() if not self.has_cycles() else []

        lines = [
            "=" * 60,
            "Build Dependency Graph Summary (DAG)",
            "=" * 60,
            f"  Executable Tasks: {total_tasks}",
            f"  Dependency Edges: {total_deps}",
            f"    - Explicit:   {explicit_deps}",
            f"    - Implicit:   {implicit_deps}",
            f"    - Order-Only: {order_only_deps}",
            f"  Phony Aliases:    {len(self.phony_targets)}",
            f"  External Sources: {len(self.source_files)}",
            f"  Default Targets:  {self.defaults}",
            f"  Execution Waves:  {len(batches)}",
        ]

        if batches:
            lines.append("\n  Execution Levels (Parallel Waves):")
            for i, batch in enumerate(batches):
                task_names = [b.primary_output for b in batch]
                lines.append(f"    Level {i} ({len(batch)} tasks): {task_names}")

        if self.warnings:
            lines.append(f"\n  Warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"    - {w}")

        lines.append("=" * 60)
        return "\n".join(lines)

    def to_mermaid(self, target: Optional[Union[str, list[str]]] = None) -> str:
        """
        Generate a GitHub-compatible Mermaid graph diagram string (graph TD).
        Styles nodes and edges:
          - Explicit:   A --> B
          - Implicit:   A -. implicit .-> B
          - Order-Only: A ==>|order-only| B
        """
        scope = self.get_target_closure(target) if target is not None else set(self.nodes.keys())
        lines = ["graph TD"]

        # Define nodes
        for tid in sorted(scope):
            node = self.nodes[tid]
            # Sanitize label for Mermaid
            label = node.primary_output.replace('"', '\\"')
            rule = node.rule_name.replace('"', '\\"')
            # Short safe ID for Mermaid
            safe_id = f"task_{abs(hash(tid)) % 100000}"
            lines.append(f'    {safe_id}["{label}\\n({rule})"]')

        # Define edges (prerequisite -> dependent, showing execution flow)
        for tid in sorted(scope):
            node = self.nodes[tid]
            safe_dept = f"task_{abs(hash(tid)) % 100000}"
            for prereq_id, dep_type in sorted(node.dependencies.items()):
                if prereq_id in scope:
                    safe_prereq = f"task_{abs(hash(prereq_id)) % 100000}"
                    if dep_type == DependencyType.EXPLICIT:
                        lines.append(f"    {safe_prereq} --> {safe_dept}")
                    elif dep_type == DependencyType.IMPLICIT:
                        lines.append(f"    {safe_prereq} -.-> {safe_dept}")
                    else:  # ORDER_ONLY
                        lines.append(f"    {safe_prereq} ==> {safe_dept}")

        return "\n".join(lines)
