"""
Data models for the Ninja-inspired build engine.

These models represent the parsed contents of a Ninja build manifest:
- BuildRule: A rule declaration (e.g., CXX_COMPILER__mylib_unscanned_)
- BuildEdge: A build statement connecting inputs to outputs via a rule
- NinjaManifest: The complete parsed manifest containing rules, edges, variables, defaults
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EdgeType(Enum):
    """Classification of build edges for our engine."""
    BUILD = "build"           # Normal build edge (compile, link)
    PHONY = "phony"           # Dependency alias — no command to execute
    CUSTOM_COMMAND = "custom_command"  # CMake custom command
    RERUN_CMAKE = "rerun_cmake"       # CMake regeneration — not supported
    CLEAN = "clean"           # Ninja clean — not supported (delegates to ninja)
    HELP = "help"             # Ninja help — not supported (delegates to ninja)


class DependencyType(Enum):
    """
    Classification of input dependencies for a task.

    - EXPLICIT: Listed before '|'. Appears in $in. Content changes trigger rebuild.
    - IMPLICIT: Listed between '|' and '||'. Content changes trigger rebuild.
    - ORDER_ONLY: Listed after '||'. Enforces execution order only; does NOT trigger rebuild.
    """
    EXPLICIT = "explicit"
    IMPLICIT = "implicit"
    ORDER_ONLY = "order_only"

    @property
    def priority(self) -> int:
        """Priority when combining dependency types for the same target."""
        if self == DependencyType.EXPLICIT:
            return 3
        if self == DependencyType.IMPLICIT:
            return 2
        return 1

    @classmethod
    def merge(cls, a: "DependencyType", b: "DependencyType") -> "DependencyType":
        """Combine two dependency types, choosing the stronger rebuild trigger."""
        return a if a.priority >= b.priority else b


# Rules that our engine does NOT execute because they delegate to
# external tools (ninja.exe, cmake) outside our scope.
UNSUPPORTED_RULES = frozenset({"RERUN_CMAKE", "CLEAN", "HELP"})


@dataclass
class BuildRule:
    """
    Represents a 'rule' declaration in a Ninja manifest.

    Example:
        rule CXX_COMPILER__mylib_unscanned_
          depfile = $DEP_FILE
          deps = gcc
          command = ...
          description = Building CXX object $out
    """
    name: str
    # Rule-level variables (command, description, depfile, deps, etc.)
    variables: dict[str, str] = field(default_factory=dict)

    @property
    def command(self) -> str:
        """The command template for this rule."""
        return self.variables.get("command", "")

    @property
    def description(self) -> str:
        """The description template for this rule."""
        return self.variables.get("description", "")

    @property
    def depfile(self) -> str:
        """The depfile variable, if any."""
        return self.variables.get("depfile", "")

    @property
    def deps(self) -> str:
        """Dependency style: 'gcc' or 'msvc'."""
        return self.variables.get("deps", "")

    @property
    def generator(self) -> bool:
        """Whether this rule has generator = 1 (CMake regeneration)."""
        return self.variables.get("generator", "") == "1"

    @property
    def restat(self) -> str:
        """The restat variable value."""
        return self.variables.get("restat", "")

    @property
    def pool(self) -> str:
        """Pool variable, if any."""
        return self.variables.get("pool", "")


@dataclass
class BuildEdge:
    """
    Represents a 'build' statement in a Ninja manifest.

    Syntax:
        build output1 output2 | implicit_out : rule explicit_in | implicit_in || order_only_in
          VARIABLE = value

    The three kinds of inputs:
    - explicit_inputs: Files that appear directly before the '|' separator.
    - implicit_inputs: Files after '|' but before '||'. Same as explicit for
      rebuild purposes, but not passed via $in.
    - order_only_inputs: Files after '||'. Must be built first, but their
      content changes do NOT trigger a rebuild of this edge.
    """
    # Outputs
    outputs: list[str] = field(default_factory=list)
    implicit_outputs: list[str] = field(default_factory=list)

    # Rule name
    rule_name: str = ""

    # Inputs (three kinds)
    explicit_inputs: list[str] = field(default_factory=list)
    implicit_inputs: list[str] = field(default_factory=list)
    order_only_inputs: list[str] = field(default_factory=list)

    # Edge-specific variable bindings (override rule variables)
    variables: dict[str, str] = field(default_factory=dict)

    # Classification
    edge_type: EdgeType = EdgeType.BUILD

    @property
    def all_outputs(self) -> list[str]:
        """All outputs (explicit + implicit)."""
        return self.outputs + self.implicit_outputs

    @property
    def all_inputs(self) -> list[str]:
        """All content-relevant inputs (explicit + implicit). NOT order-only."""
        return self.explicit_inputs + self.implicit_inputs

    @property
    def all_dependencies(self) -> list[str]:
        """All inputs including order-only (for scheduling purposes)."""
        return self.explicit_inputs + self.implicit_inputs + self.order_only_inputs

    @property
    def is_phony(self) -> bool:
        return self.rule_name == "phony"

    @property
    def primary_output(self) -> str:
        """The first (primary) output, used as a convenient identifier."""
        return self.outputs[0] if self.outputs else ""


@dataclass
class NinjaManifest:
    """
    The complete parsed result of a Ninja build manifest.

    Contains:
    - Top-level variables
    - Rule declarations
    - Build edges
    - Default targets
    - Warnings about unsupported constructs
    """
    # Top-level variable bindings
    variables: dict[str, str] = field(default_factory=dict)

    # Rule name -> BuildRule
    rules: dict[str, BuildRule] = field(default_factory=dict)

    # All build edges in declaration order
    edges: list[BuildEdge] = field(default_factory=list)

    # Default targets (from 'default' statement)
    defaults: list[str] = field(default_factory=list)

    # Warnings generated during parsing (unsupported features, etc.)
    warnings: list[str] = field(default_factory=list)

    # Paths of included/subninja files that were processed
    included_files: list[str] = field(default_factory=list)

    def get_rule(self, name: str) -> Optional[BuildRule]:
        """Look up a rule by name."""
        return self.rules.get(name)

    def get_build_edges(self) -> list[BuildEdge]:
        """Return only edges that represent actual build work (not phony aliases)."""
        return [e for e in self.edges if e.edge_type == EdgeType.BUILD]

    def get_phony_edges(self) -> list[BuildEdge]:
        """Return phony (alias) edges."""
        return [e for e in self.edges if e.edge_type == EdgeType.PHONY]

    def get_unsupported_edges(self) -> list[BuildEdge]:
        """Return edges using unsupported rules (RERUN_CMAKE, CLEAN, HELP)."""
        return [e for e in self.edges
                if e.edge_type in (EdgeType.RERUN_CMAKE, EdgeType.CLEAN, EdgeType.HELP)]

    def summary(self) -> str:
        """Human-readable summary of the parsed manifest."""
        lines = [
            f"Ninja Manifest Summary",
            f"  Top-level variables: {len(self.variables)}",
            f"  Rules: {len(self.rules)}",
            f"  Build edges: {len(self.edges)}",
            f"    - Normal build: {len(self.get_build_edges())}",
            f"    - Phony aliases: {len(self.get_phony_edges())}",
            f"    - Unsupported: {len(self.get_unsupported_edges())}",
            f"  Default targets: {self.defaults}",
            f"  Included files: {self.included_files}",
        ]
        if self.warnings:
            lines.append(f"  Warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"    - {w}")
        return "\n".join(lines)


@dataclass
class TaskNode:
    """
    Represents an executable build task in the Dependency Graph (DAG).

    Each task corresponds to a concrete BuildEdge that executes a command
    (e.g., C++ compilation, archive creation, executable linking).
    Phony aliases are resolved into real tasks and are not nodes in the DAG.
    """
    task_id: str
    rule_name: str
    command: str = ""
    description: str = ""
    outputs: list[str] = field(default_factory=list)
    implicit_outputs: list[str] = field(default_factory=list)
    explicit_inputs: list[str] = field(default_factory=list)
    implicit_inputs: list[str] = field(default_factory=list)
    order_only_inputs: list[str] = field(default_factory=list)

    # Prerequisite tasks: task_id -> DependencyType (EXPLICIT, IMPLICIT, ORDER_ONLY)
    dependencies: dict[str, DependencyType] = field(default_factory=dict)

    # Downstream dependent tasks: set of task_ids that depend on this task
    dependents: set[str] = field(default_factory=set)

    # Inputs that come directly from source files (no producing task in the DAG)
    # Stored as list of (normalized_file_path, DependencyType)
    source_inputs: list[tuple[str, DependencyType]] = field(default_factory=list)

    # Variables
    variables: dict[str, str] = field(default_factory=dict)
    rule_variables: dict[str, str] = field(default_factory=dict)

    @property
    def all_outputs(self) -> list[str]:
        """All outputs generated by this task (explicit + implicit)."""
        return self.outputs + self.implicit_outputs

    @property
    def primary_output(self) -> str:
        """The primary output file, or task_id if no outputs."""
        return self.outputs[0] if self.outputs else self.task_id

    @property
    def all_inputs(self) -> list[str]:
        """All content-relevant inputs (explicit + implicit, excluding order-only)."""
        return self.explicit_inputs + self.implicit_inputs

    @property
    def all_dependencies(self) -> set[str]:
        """Set of all prerequisite task IDs."""
        return set(self.dependencies.keys())

    @property
    def rebuild_dependencies(self) -> dict[str, DependencyType]:
        """Prerequisites whose change triggers a rebuild (explicit & implicit)."""
        return {
            tid: dtype for tid, dtype in self.dependencies.items()
            if dtype != DependencyType.ORDER_ONLY
        }

    @property
    def order_only_dependencies(self) -> dict[str, DependencyType]:
        """Prerequisites that only enforce topological execution order."""
        return {
            tid: dtype for tid, dtype in self.dependencies.items()
            if dtype == DependencyType.ORDER_ONLY
        }

