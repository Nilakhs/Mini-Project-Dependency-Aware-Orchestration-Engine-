"""
E2 — Verifying-Trace Rebuilder

Determines whether build tasks need to execute again by comparing current
task fingerprints against persistent records of previous successful builds.

Architecture & Responsibilities:
- Computes SHA-256 fingerprints for task inputs, commands, outputs, and dependencies.
- Respects dependency semantics:
    * EXPLICIT & IMPLICIT inputs/dependencies trigger rebuilds.
    * ORDER_ONLY dependencies enforce scheduling order only; changes do NOT trigger rebuilds.
- Verifies output existence and output integrity on disk.
- Consumes dynamic compiler depfiles (.d) when available for header tracking.
- Updates persistent SQLite build traces ONLY after successful execution.
- Does NOT execute subprocesses or schedule tasks (pure decision engine).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

from engine.cache import BuildCache, BuildTrace, CacheError
from engine.dag import BuildGraph, normalize_path
from engine.models import DependencyType, TaskNode


class RebuildAction(Enum):
    """Execution action determined for a task."""
    BUILD = "BUILD"
    SKIP = "SKIP"


class RebuildReason(Enum):
    """Reason for the rebuild decision."""
    FIRST_BUILD = "no previous successful trace"
    MISSING_INPUT = "required input file is missing or unreadable"
    OUTPUT_MISSING = "expected output file does not exist on disk"
    OUTPUT_MODIFIED = "output file fingerprint mismatch (externally modified)"
    COMMAND_CHANGED = "build command or rule variables changed"
    INPUT_CHANGED = "input file content fingerprint changed"
    DEPENDENCY_CHANGED = "relevant upstream dependency was rebuilt or changed"
    UP_TO_DATE = "all relevant trace checks pass"


@dataclass
class RebuildDecision:
    """Structured decision explaining whether a task should build or skip."""
    task_id: str
    action: RebuildAction
    reason: RebuildReason
    details: str = ""
    current_fingerprint: str = ""
    previous_fingerprint: Optional[str] = None
    output_fingerprints: dict[str, str] = field(default_factory=dict)

    @property
    def should_build(self) -> bool:
        return self.action == RebuildAction.BUILD

    @property
    def should_skip(self) -> bool:
        return self.action == RebuildAction.SKIP


# ─────────────────────────────────────────────────────────────
# Cryptographic Hashing Utilities (SHA-256)
# ─────────────────────────────────────────────────────────────

def hash_string(text: str) -> str:
    """Compute the SHA-256 hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_file(file_path: str) -> Optional[str]:
    """
    Compute the SHA-256 hex digest of a file's content.
    Reads in 64 KB chunks to handle files of any size efficiently.
    Returns None if the file does not exist or is unreadable.
    """
    if not os.path.isfile(file_path):
        return None
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


# ─────────────────────────────────────────────────────────────
# Depfile Parser (.d Makefile-style dependencies)
# ─────────────────────────────────────────────────────────────

def parse_depfile(depfile_path: str) -> list[str]:
    """
    Parse a GCC/Clang generated Makefile-style depfile (.d).
    Format:
        target.o: target.cpp target.h \\
          header1.h \\
          header2.h

    Handles:
    - Escaped spaces (\\ ) in paths
    - Windows drive letters (C:) in target and prerequisites
    - Line continuations (\\ followed by newline)
    """
    if not os.path.isfile(depfile_path):
        return []

    try:
        with open(depfile_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return []

    # Strip continuation backslashes
    content = content.replace("\\\r\n", " ").replace("\\\n", " ")

    # Find the rule colon separating target from prerequisites.
    # In Windows, drive letters (e.g. C:) appear at index 1 or after a slash.
    # The rule separator colon is followed by whitespace.
    colon_idx = -1
    for i, ch in enumerate(content):
        if ch == ":":
            if i == 1 and content[0].isalpha():
                continue  # Windows drive letter C:
            if i + 1 < len(content) and content[i + 1] in (" ", "\t", "\r", "\n"):
                colon_idx = i
                break

    if colon_idx < 0:
        colon_idx = content.find(":")
        if colon_idx < 0:
            return []

    prereqs_str = content[colon_idx + 1:].strip()

    # Tokenize prereqs handling escaped spaces ('\\ ')
    deps: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(prereqs_str):
        ch = prereqs_str[i]
        if ch == "\\" and i + 1 < len(prereqs_str) and prereqs_str[i + 1] == " ":
            current.append(" ")
            i += 2
        elif ch in (" ", "\t", "\r", "\n"):
            if current:
                token = "".join(current).strip()
                if token and not token.endswith(".obj") and not token.endswith(".o"):
                    deps.append(normalize_path(token))
                current = []
            i += 1
        else:
            current.append(ch)
            i += 1

    if current:
        token = "".join(current).strip()
        if token and not token.endswith(".obj") and not token.endswith(".o"):
            deps.append(normalize_path(token))

    return deps


# ─────────────────────────────────────────────────────────────
# Verifying-Trace Rebuilder
# ─────────────────────────────────────────────────────────────

class Rebuilder:
    """
    Verifying-Trace Rebuilder.

    Implements the Rebuilder dimension from 'Build Systems à la Carte'.
    Compares current task fingerprints with persistent records in SQLite.
    """

    def __init__(self, cache: BuildCache, base_dir: str = ""):
        self.cache = cache
        self.base_dir = base_dir

    def _resolve_path(self, path: str) -> str:
        """Resolve a path relative to the base directory if not absolute."""
        if os.path.isabs(path):
            return path
        if self.base_dir:
            return os.path.normpath(os.path.join(self.base_dir, path))
        return path

    # ─────────────────────────────────────────────────────────
    # Fingerprint Computation
    # ─────────────────────────────────────────────────────────

    def compute_command_hash(self, task: TaskNode) -> str:
        """
        Compute deterministic SHA-256 hash of the task's command and configuration.
        Accounts for:
        - rule_name
        - fully resolved command string
        - edge variables (sorted)
        - rule variables (sorted)
        """
        config_payload = {
            "rule": task.rule_name,
            "command": task.command,
            "variables": sorted(task.variables.items()),
            "rule_variables": sorted(task.rule_variables.items()),
        }
        serialized = json.dumps(config_payload, sort_keys=True)
        return hash_string(serialized)

    def compute_input_fingerprint(
        self, task: TaskNode, graph: BuildGraph
    ) -> tuple[str, dict[str, str], list[str]]:
        """
        Compute deterministic SHA-256 fingerprint for all content-relevant inputs.

        Includes:
        - Explicit inputs (source files, prerequisite outputs)
        - Implicit inputs
        - Dynamic header dependencies from depfile (.d), if present

        EXCLUDES:
        - Order-only inputs (order-only dependencies do NOT trigger rebuilds).

        Returns:
            (input_fingerprint, file_hashes_dict, missing_inputs_list)
        """
        file_hashes: dict[str, str] = {}
        missing_inputs: list[str] = []

        # Content-relevant inputs: explicit + implicit (NOT order-only!)
        relevant_inputs = list(task.explicit_inputs + task.implicit_inputs)

        # Check for dynamic compiler depfile (.d)
        depfile_var = task.variables.get("DEP_FILE") or task.rule_variables.get("depfile")
        if depfile_var:
            resolved_depfile = self._resolve_path(depfile_var)
            header_deps = parse_depfile(resolved_depfile)
            for h in header_deps:
                if h not in relevant_inputs:
                    relevant_inputs.append(h)

        # Hash each file deterministically
        for inp in relevant_inputs:
            resolved = self._resolve_path(inp)
            h = hash_file(resolved)
            if h is None:
                # If the input is produced by a prerequisite task in the graph,
                # it might not exist yet before clean builds. That's fine; it will be marked missing.
                missing_inputs.append(inp)
            else:
                norm_key = normalize_path(inp)
                file_hashes[norm_key] = h

        # Deterministic serialization of all input hashes
        serialized_inputs = json.dumps(
            sorted(file_hashes.items()), sort_keys=True
        )
        input_fingerprint = hash_string(serialized_inputs)
        return input_fingerprint, file_hashes, missing_inputs

    def compute_full_task_fingerprint(
        self,
        task: TaskNode,
        input_fingerprint: str,
        command_hash: str,
        prereq_fingerprints: dict[str, str],
    ) -> str:
        """
        Compute full task fingerprint combining:
        - Command hash
        - Input fingerprint
        - Prerequisite task fingerprints (for EXPLICIT and IMPLICIT dependencies ONLY)
        - Task output paths
        """
        # Only include prerequisite fingerprints for rebuild dependencies (not order-only!)
        relevant_prereq_fingerprints = {
            tid: prereq_fingerprints[tid]
            for tid in sorted(task.rebuild_dependencies.keys())
            if tid in prereq_fingerprints
        }

        full_payload = {
            "task_id": task.task_id,
            "outputs": sorted(normalize_path(o) for o in task.all_outputs),
            "command_hash": command_hash,
            "input_fingerprint": input_fingerprint,
            "prerequisites": sorted(relevant_prereq_fingerprints.items()),
        }
        return hash_string(json.dumps(full_payload, sort_keys=True))

    def compute_output_fingerprints(self, task: TaskNode) -> dict[str, Optional[str]]:
        """
        Compute SHA-256 for all outputs generated by this task.
        Returns dict of output_path -> sha256 (or None if missing).
        """
        results: dict[str, Optional[str]] = {}
        for out in task.all_outputs:
            resolved = self._resolve_path(out)
            results[normalize_path(out)] = hash_file(resolved)
        return results

    # ─────────────────────────────────────────────────────────
    # Task Evaluation
    # ─────────────────────────────────────────────────────────

    def evaluate_task(
        self,
        task: TaskNode,
        graph: BuildGraph,
        upstream_decisions: Optional[dict[str, RebuildDecision]] = None,
        upstream_fingerprints: Optional[dict[str, str]] = None,
    ) -> RebuildDecision:
        """
        Decide whether a task should BUILD or SKIP.

        Checks:
        1. Upstream rebuilds: Did any prerequisite (EXPLICIT/IMPLICIT) rebuild?
        2. Previous trace: Does a recorded successful trace exist in SQLite?
        3. Missing inputs: Are any required source files missing on disk?
        4. Outputs on disk: Do all expected output files exist?
        5. Output integrity: Do output file fingerprints match recorded traces?
        6. Command check: Has the command string or rule configuration changed?
        7. Input check: Have any input file contents changed?
        8. Upstream fingerprints: Have prerequisite task fingerprints changed?
        """
        upstream_decisions = upstream_decisions or {}
        upstream_fingerprints = upstream_fingerprints or {}

        # ── Check 1: Did an upstream rebuild dependency rebuild in this session? ──
        for prereq_id in task.rebuild_dependencies:
            if prereq_id in upstream_decisions:
                decision = upstream_decisions[prereq_id]
                if decision.should_build:
                    return RebuildDecision(
                        task_id=task.task_id,
                        action=RebuildAction.BUILD,
                        reason=RebuildReason.DEPENDENCY_CHANGED,
                        details=f"Upstream prerequisite '{prereq_id}' was rebuilt ({decision.reason.value}).",
                    )

        # ── Check 2: Retrieve previous build trace from SQLite cache ──
        prev_trace = self.cache.get_trace(task.task_id)
        if prev_trace is None:
            return RebuildDecision(
                task_id=task.task_id,
                action=RebuildAction.BUILD,
                reason=RebuildReason.FIRST_BUILD,
                details="No previous build trace recorded in database.",
            )

        # ── Check 3: Check for missing required inputs ──
        input_fp, input_hashes, missing_inputs = self.compute_input_fingerprint(
            task, graph
        )
        if missing_inputs:
            # Check if missing inputs are external source files
            missing_sources = [
                inp for inp in missing_inputs
                if normalize_path(inp) in graph.source_files
            ]
            if missing_sources:
                return RebuildDecision(
                    task_id=task.task_id,
                    action=RebuildAction.BUILD,
                    reason=RebuildReason.MISSING_INPUT,
                    details=f"Required source file missing: {missing_sources[0]}",
                    previous_fingerprint=prev_trace.full_fingerprint,
                )

        # ── Check 4: Check if outputs exist on disk ──
        curr_output_fps = self.compute_output_fingerprints(task)
        for out_path, out_hash in curr_output_fps.items():
            if out_hash is None:
                return RebuildDecision(
                    task_id=task.task_id,
                    action=RebuildAction.BUILD,
                    reason=RebuildReason.OUTPUT_MISSING,
                    details=f"Expected output file '{out_path}' does not exist on disk.",
                    previous_fingerprint=prev_trace.full_fingerprint,
                )

        # ── Check 5: Output file integrity (detect external modifications) ──
        for out_path, curr_hash in curr_output_fps.items():
            recorded_hash = prev_trace.output_fingerprints.get(out_path)
            if recorded_hash and curr_hash != recorded_hash:
                return RebuildDecision(
                    task_id=task.task_id,
                    action=RebuildAction.BUILD,
                    reason=RebuildReason.OUTPUT_MODIFIED,
                    details=f"Output file '{out_path}' modified externally (SHA-256 mismatch).",
                    previous_fingerprint=prev_trace.full_fingerprint,
                )

        # ── Check 6: Check command and rule variables ──
        cmd_hash = self.compute_command_hash(task)
        if cmd_hash != prev_trace.command_hash:
            return RebuildDecision(
                task_id=task.task_id,
                action=RebuildAction.BUILD,
                reason=RebuildReason.COMMAND_CHANGED,
                details="Build command, compiler flags, or rule variables changed.",
                previous_fingerprint=prev_trace.full_fingerprint,
            )

        # ── Check 7: Check input content fingerprint ──
        if input_fp != prev_trace.input_fingerprint:
            return RebuildDecision(
                task_id=task.task_id,
                action=RebuildAction.BUILD,
                reason=RebuildReason.INPUT_CHANGED,
                details="One or more input files were modified (content SHA-256 changed).",
                current_fingerprint=input_fp,
                previous_fingerprint=prev_trace.input_fingerprint,
            )

        # ── Check 8: Check prerequisite task fingerprints ──
        for prereq_id in task.rebuild_dependencies:
            prereq_trace = self.cache.get_trace(prereq_id)
            if prereq_trace is None:
                return RebuildDecision(
                    task_id=task.task_id,
                    action=RebuildAction.BUILD,
                    reason=RebuildReason.DEPENDENCY_CHANGED,
                    details=f"Prerequisite '{prereq_id}' has no recorded trace.",
                    previous_fingerprint=prev_trace.full_fingerprint,
                )

        # ── All checks passed: task is completely up to date! ──
        full_fp = self.compute_full_task_fingerprint(
            task, input_fp, cmd_hash, upstream_fingerprints
        )
        return RebuildDecision(
            task_id=task.task_id,
            action=RebuildAction.SKIP,
            reason=RebuildReason.UP_TO_DATE,
            details="All inputs, outputs, commands, and dependency traces are verified.",
            current_fingerprint=full_fp,
            previous_fingerprint=prev_trace.full_fingerprint,
            output_fingerprints={k: v for k, v in curr_output_fps.items() if v},
        )

    # ─────────────────────────────────────────────────────────
    # Plan Evaluation (Topological Sweep)
    # ─────────────────────────────────────────────────────────

    def evaluate_plan(
        self,
        graph: BuildGraph,
        target: Optional[Union[str, list[str]]] = None,
    ) -> dict[str, RebuildDecision]:
        """
        Evaluate an entire build plan in topological order.

        Traverses tasks in prerequisite order, allowing rebuild decisions
        to propagate downstream to dependent tasks.

        Returns:
            Dict mapping task_id -> RebuildDecision
        """
        # Get target closure tasks in topological execution order
        ordered_tasks = graph.topological_sort_for_target(target)

        decisions: dict[str, RebuildDecision] = {}
        fingerprints: dict[str, str] = {}

        for task in ordered_tasks:
            decision = self.evaluate_task(
                task,
                graph,
                upstream_decisions=decisions,
                upstream_fingerprints=fingerprints,
            )
            decisions[task.task_id] = decision
            if decision.current_fingerprint:
                fingerprints[task.task_id] = decision.current_fingerprint

        return decisions

    # ─────────────────────────────────────────────────────────
    # Post-Execution Trace Recording
    # ─────────────────────────────────────────────────────────

    def record_success(
        self,
        task: TaskNode,
        graph: BuildGraph,
        prereq_fingerprints: Optional[dict[str, str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> BuildTrace:
        """
        Record a successful task execution in the cache.

        Validates outputs on disk, hashes them, computes fingerprints,
        and atomically stores the trace in SQLite.
        Must ONLY be called after successful task execution.
        """
        prereq_fingerprints = prereq_fingerprints or {}

        # 1. Output hashes
        output_fps = self.compute_output_fingerprints(task)
        valid_output_fps: dict[str, str] = {}
        for out_path, h in output_fps.items():
            if h is None:
                raise CacheError(
                    f"Cannot record successful trace for '{task.task_id}': "
                    f"output file '{out_path}' is missing on disk."
                )
            valid_output_fps[out_path] = h

        # 2. Input hashes
        input_fp, _, _ = self.compute_input_fingerprint(task, graph)

        # 3. Command hash
        cmd_hash = self.compute_command_hash(task)

        # 4. Full fingerprint
        full_fp = self.compute_full_task_fingerprint(
            task, input_fp, cmd_hash, prereq_fingerprints
        )

        trace = BuildTrace(
            task_id=task.task_id,
            rule_name=task.rule_name,
            command_hash=cmd_hash,
            input_fingerprint=input_fp,
            full_fingerprint=full_fp,
            output_fingerprints=valid_output_fps,
            build_timestamp=time.time(),
            metadata=metadata or {},
        )

        self.cache.record_success(trace)
        return trace
