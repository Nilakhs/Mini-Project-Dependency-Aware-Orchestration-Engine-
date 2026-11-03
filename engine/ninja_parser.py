"""
Ninja Manifest Parser

Parses a real CMake-generated build.ninja file into our data models.

Supported Ninja syntax (sufficient for CMake-generated manifests):
  - Variable assignments: name = value
  - Rule declarations: rule <name> + indented variables
  - Build statements: build <outputs> : <rule> <inputs> | <implicit> || <order-only>
  - Edge-specific variables (indented under build)
  - Variable expansion: $var, ${var}, $in, $out
  - Escape sequences: $$ (literal $), $: (literal :), $  (literal space, $ followed by space)
  - Line continuations: $ at end of line
  - include <path>  (merged into current scope)
  - subninja <path> (new variable scope — parsed but variables don't leak)
  - default <targets>
  - Comments: lines starting with #
  - phony rule (treated as dependency alias)

Unsupported (detected and reported):
  - pool declarations
  - RERUN_CMAKE / CLEAN / HELP rules (detected, edges classified, warning issued)

Does NOT use eval() or execute manifest text as Python.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from engine.models import (
    BuildEdge, BuildRule, EdgeType, NinjaManifest, UNSUPPORTED_RULES,
)


class NinjaParserError(Exception):
    """Raised when the parser encounters an unrecoverable syntax error."""

    def __init__(self, message: str, file: str = "", line_num: int = 0):
        self.file = file
        self.line_num = line_num
        location = f" ({file}:{line_num})" if file else ""
        super().__init__(f"{message}{location}")


class NinjaParser:
    """
    Parses Ninja build manifests (.ninja files).

    Usage:
        parser = NinjaParser()
        manifest = parser.parse_file("path/to/build.ninja")
    """

    def __init__(self):
        self._manifest = NinjaManifest()
        self._base_dir: str = ""  # Directory of the root manifest (for resolving includes)

    # ─────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────

    def parse_file(self, path: str) -> NinjaManifest:
        """
        Parse a Ninja manifest file and return the populated NinjaManifest.

        Args:
            path: Path to the build.ninja file.

        Returns:
            NinjaManifest with all rules, edges, variables, defaults, and warnings.
        """
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise NinjaParserError(f"Manifest file not found: {path}")

        self._manifest = NinjaManifest()
        self._base_dir = os.path.dirname(path)

        self._parse_file_impl(path, self._manifest.variables)
        return self._manifest

    def parse_string(self, content: str, variables: Optional[dict[str, str]] = None) -> NinjaManifest:
        """
        Parse a Ninja manifest from a string. Useful for testing.

        Args:
            content: The manifest text.
            variables: Optional pre-set variable scope.

        Returns:
            NinjaManifest.
        """
        self._manifest = NinjaManifest()
        self._base_dir = ""
        if variables:
            self._manifest.variables.update(variables)

        lines = self._preprocess(content, "<string>")
        self._parse_lines(lines, self._manifest.variables, "<string>")
        return self._manifest

    # ─────────────────────────────────────────────────────
    # Preprocessing: handle line continuations
    # ─────────────────────────────────────────────────────

    def _preprocess(self, text: str, source_file: str) -> list[tuple[int, str]]:
        """
        Split text into logical lines, joining continuation lines.

        A line ending with '$' (Ninja escape) means the next line is a
        continuation. The '$\\n' is replaced with nothing, and leading
        whitespace on the continuation line is collapsed to a single space.

        Returns list of (original_line_number, logical_line).
        """
        raw_lines = text.split("\n")
        result: list[tuple[int, str]] = []
        i = 0
        while i < len(raw_lines):
            line = raw_lines[i]
            # Remove trailing \r for Windows
            if line.endswith("\r"):
                line = line[:-1]
            start_line = i + 1  # 1-based

            # Join continuation lines: line ending with $ (not $$)
            while self._is_continuation(line) and i + 1 < len(raw_lines):
                # Remove the trailing $
                line = line[:-1]
                i += 1
                next_line = raw_lines[i]
                if next_line.endswith("\r"):
                    next_line = next_line[:-1]
                # Strip leading whitespace from continuation, add single space
                line = line + " " + next_line.lstrip()

            result.append((start_line, line))
            i += 1
        return result

    def _is_continuation(self, line: str) -> bool:
        """
        Check if a line ends with a continuation marker.

        A line is continued if it ends with '$' but NOT '$$'.
        We need to count trailing '$' signs — odd count means continuation.
        """
        if not line.endswith("$"):
            return False
        # Count trailing dollar signs
        count = 0
        for ch in reversed(line):
            if ch == "$":
                count += 1
            else:
                break
        # Odd number of trailing $ means the last one is a continuation
        return count % 2 == 1

    # ─────────────────────────────────────────────────────
    # File parsing
    # ─────────────────────────────────────────────────────

    def _parse_file_impl(self, path: str, scope: dict[str, str]) -> None:
        """Read and parse a single .ninja file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            raise NinjaParserError(f"Cannot read file: {e}", file=path)

        lines = self._preprocess(content, path)
        self._parse_lines(lines, scope, path)

    def _parse_lines(
        self, lines: list[tuple[int, str]], scope: dict[str, str], source_file: str
    ) -> None:
        """
        Parse a sequence of logical lines.

        The main parsing loop that dispatches to rule, build, include, etc.
        """
        idx = 0
        while idx < len(lines):
            line_num, line = lines[idx]

            # Skip empty lines and comments
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                idx += 1
                continue

            # ── rule declaration ──
            if stripped.startswith("rule "):
                rule_name = stripped[5:].strip()
                if not rule_name:
                    raise NinjaParserError("Empty rule name", source_file, line_num)
                rule = BuildRule(name=rule_name)
                idx += 1
                # Collect indented variables
                idx = self._parse_indented_vars(lines, idx, rule.variables, source_file)
                self._manifest.rules[rule_name] = rule
                continue

            # ── build statement ──
            if stripped.startswith("build "):
                edge, idx = self._parse_build(lines, idx, scope, source_file)
                self._manifest.edges.append(edge)
                continue

            # ── default statement ──
            if stripped.startswith("default "):
                targets = stripped[8:].strip()
                if targets:
                    for target in self._split_ninja_paths(targets):
                        decoded = self._decode_ninja_path(target)
                        self._manifest.defaults.append(decoded)
                idx += 1
                continue

            # ── include statement ──
            if stripped.startswith("include "):
                inc_path = stripped[8:].strip()
                self._handle_include(inc_path, scope, source_file, line_num)
                idx += 1
                continue

            # ── subninja statement ──
            if stripped.startswith("subninja "):
                inc_path = stripped[9:].strip()
                self._handle_subninja(inc_path, scope, source_file, line_num)
                idx += 1
                continue

            # ── pool declaration (unsupported — warn) ──
            if stripped.startswith("pool "):
                pool_name = stripped[5:].strip()
                self._manifest.warnings.append(
                    f"Pool '{pool_name}' is declared but pool support is not implemented. "
                    f"({source_file}:{line_num})"
                )
                idx += 1
                # Skip indented variables under the pool
                while idx < len(lines):
                    _, next_line = lines[idx]
                    if next_line and (next_line[0] == " " or next_line[0] == "\t"):
                        idx += 1
                    else:
                        break
                continue

            # ── variable assignment ──
            eq_pos = stripped.find("=")
            if eq_pos > 0 and not stripped[0].isspace():
                var_name = stripped[:eq_pos].rstrip()
                var_value = stripped[eq_pos + 1:].lstrip()
                # Only accept valid identifier-like names
                if self._is_valid_var_name(var_name):
                    scope[var_name] = var_value
                    idx += 1
                    continue

            # ── unrecognized line ──
            self._manifest.warnings.append(
                f"Unrecognized statement: '{stripped[:80]}' ({source_file}:{line_num})"
            )
            idx += 1

    # ─────────────────────────────────────────────────────
    # Build statement parsing
    # ─────────────────────────────────────────────────────

    def _parse_build(
        self,
        lines: list[tuple[int, str]],
        idx: int,
        scope: dict[str, str],
        source_file: str,
    ) -> tuple[BuildEdge, int]:
        """
        Parse a build statement and its indented variable block.

        Format:
            build out1 out2 | implicit_out : rulename in1 in2 | implicit_in || order_only
              VAR = value
        """
        line_num, line = lines[idx]
        # Strip 'build ' prefix
        build_body = line[line.index("build ") + 6:]

        # Split at the (unescaped) colon that separates outputs from rule+inputs.
        # Ninja syntax: colon is the separator, but '$:' is an escaped literal colon.
        colon_pos = self._find_unescaped_colon(build_body)
        if colon_pos < 0:
            raise NinjaParserError(
                f"Build statement missing ':' separator", source_file, line_num
            )

        outputs_part = build_body[:colon_pos].strip()
        rule_and_inputs = build_body[colon_pos + 1:].strip()

        # Parse outputs: split by '|' for implicit outputs
        outputs, implicit_outputs = self._split_outputs(outputs_part)

        # Parse rule name and inputs
        parts = rule_and_inputs.split(None, 1)
        rule_name = parts[0] if parts else ""
        inputs_str = parts[1] if len(parts) > 1 else ""

        # Parse inputs: explicit | implicit || order-only
        explicit_inputs, implicit_inputs, order_only_inputs = self._split_inputs(inputs_str)

        # Decode ninja path escapes on all paths
        outputs = [self._decode_ninja_path(p) for p in outputs]
        implicit_outputs = [self._decode_ninja_path(p) for p in implicit_outputs]
        explicit_inputs = [self._decode_ninja_path(p) for p in explicit_inputs]
        implicit_inputs = [self._decode_ninja_path(p) for p in implicit_inputs]
        order_only_inputs = [self._decode_ninja_path(p) for p in order_only_inputs]

        # Classify the edge
        edge_type = self._classify_edge(rule_name)

        edge = BuildEdge(
            outputs=outputs,
            implicit_outputs=implicit_outputs,
            rule_name=rule_name,
            explicit_inputs=explicit_inputs,
            implicit_inputs=implicit_inputs,
            order_only_inputs=order_only_inputs,
            edge_type=edge_type,
        )

        idx += 1

        # Parse indented edge-specific variables
        idx = self._parse_indented_vars(lines, idx, edge.variables, source_file)

        # Generate warnings for unsupported edges
        if rule_name in UNSUPPORTED_RULES:
            self._manifest.warnings.append(
                f"Build edge uses unsupported rule '{rule_name}'. "
                f"Outputs: {outputs}. This edge will be detected but not executed by our engine. "
                f"({source_file}:{line_num})"
            )

        return edge, idx

    def _find_unescaped_colon(self, text: str) -> int:
        """
        Find the position of the first unescaped colon.

        In Ninja syntax, '$:' is a literal colon (not a separator).
        We need the first ':' that is NOT preceded by '$'.
        """
        i = 0
        while i < len(text):
            if text[i] == "$":
                # Skip the next character (it's escaped)
                i += 2
                continue
            if text[i] == ":":
                return i
            i += 1
        return -1

    def _split_outputs(self, text: str) -> tuple[list[str], list[str]]:
        """
        Split outputs part at '|' into explicit and implicit outputs.

        In Ninja, outputs can have implicit outputs after '|':
            out1 out2 | implicit_out1
        """
        parts = self._split_at_pipe(text)
        explicit = self._split_ninja_paths(parts[0]) if parts[0].strip() else []
        implicit = self._split_ninja_paths(parts[1]) if len(parts) > 1 and parts[1].strip() else []
        return explicit, implicit

    def _split_inputs(self, text: str) -> tuple[list[str], list[str], list[str]]:
        """
        Split inputs into explicit, implicit (after |), and order-only (after ||).

        Format: explicit_inputs | implicit_inputs || order_only_inputs
        """
        # First split by '||' for order-only
        order_only: list[str] = []
        double_pipe_pos = text.find("||")
        if double_pipe_pos >= 0:
            order_only_str = text[double_pipe_pos + 2:].strip()
            text = text[:double_pipe_pos].strip()
            if order_only_str:
                order_only = self._split_ninja_paths(order_only_str)

        # Then split by '|' for implicit inputs
        implicit: list[str] = []
        pipe_pos = text.find("|")
        if pipe_pos >= 0:
            implicit_str = text[pipe_pos + 1:].strip()
            text = text[:pipe_pos].strip()
            if implicit_str:
                implicit = self._split_ninja_paths(implicit_str)

        # Remaining is explicit inputs
        explicit = self._split_ninja_paths(text) if text.strip() else []

        return explicit, implicit, order_only

    def _split_at_pipe(self, text: str) -> list[str]:
        """Split at '|' but not '||'."""
        # Find single | that is not part of ||
        result = []
        current = ""
        i = 0
        while i < len(text):
            if text[i] == "|" and i + 1 < len(text) and text[i + 1] == "|":
                # This is ||, include it literally for now
                current += "||"
                i += 2
            elif text[i] == "|":
                result.append(current)
                current = ""
                i += 1
            else:
                current += text[i]
                i += 1
        result.append(current)
        return result

    def _split_ninja_paths(self, text: str) -> list[str]:
        """
        Split a space-separated list of Ninja paths.

        Ninja paths can contain escaped spaces ('$ '), so we can't just
        use str.split(). We need to split on unescaped spaces only.
        """
        paths: list[str] = []
        current: list[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "$" and i + 1 < len(text):
                next_ch = text[i + 1]
                if next_ch == " ":
                    # Escaped space — part of the path
                    current.append("$ ")
                    i += 2
                    continue
                elif next_ch == "$":
                    current.append("$$")
                    i += 2
                    continue
                elif next_ch == ":":
                    current.append("$:")
                    i += 2
                    continue
                else:
                    current.append(ch)
                    i += 1
                    continue
            elif ch == " " or ch == "\t":
                # Unescaped space — path boundary
                if current:
                    paths.append("".join(current))
                    current = []
                i += 1
                continue
            else:
                current.append(ch)
                i += 1
        if current:
            paths.append("".join(current))
        return paths

    def _decode_ninja_path(self, path: str) -> str:
        """
        Decode Ninja escape sequences in a path.

        '$:' -> ':'
        '$ ' -> ' '
        '$$' -> '$'
        """
        result: list[str] = []
        i = 0
        while i < len(path):
            if path[i] == "$" and i + 1 < len(path):
                next_ch = path[i + 1]
                if next_ch == ":":
                    result.append(":")
                    i += 2
                elif next_ch == " ":
                    result.append(" ")
                    i += 2
                elif next_ch == "$":
                    result.append("$")
                    i += 2
                else:
                    result.append(path[i])
                    i += 1
            else:
                result.append(path[i])
                i += 1
        return "".join(result)

    # ─────────────────────────────────────────────────────
    # Indented variable blocks
    # ─────────────────────────────────────────────────────

    def _parse_indented_vars(
        self,
        lines: list[tuple[int, str]],
        idx: int,
        target: dict[str, str],
        source_file: str,
    ) -> int:
        """
        Parse indented 'key = value' lines that follow a rule or build statement.

        Returns the index of the first non-indented line.
        """
        while idx < len(lines):
            line_num, line = lines[idx]
            # Indented line starts with space or tab
            if not line or (line[0] != " " and line[0] != "\t"):
                break
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                idx += 1
                continue
            eq_pos = stripped.find("=")
            if eq_pos > 0:
                var_name = stripped[:eq_pos].rstrip()
                var_value = stripped[eq_pos + 1:].lstrip()
                target[var_name] = var_value
            else:
                self._manifest.warnings.append(
                    f"Expected 'variable = value' in indented block, got: "
                    f"'{stripped[:60]}' ({source_file}:{line_num})"
                )
            idx += 1
        return idx

    # ─────────────────────────────────────────────────────
    # Include / subninja
    # ─────────────────────────────────────────────────────

    def _handle_include(
        self, rel_path: str, scope: dict[str, str], source_file: str, line_num: int
    ) -> None:
        """
        Handle 'include path' — parsed in the current scope.

        Rules and variables from the included file are merged into the
        current manifest. This is how CMake puts rules in CMakeFiles/rules.ninja.
        """
        abs_path = self._resolve_path(rel_path, source_file)
        if not os.path.isfile(abs_path):
            raise NinjaParserError(
                f"Included file not found: {rel_path} (resolved to {abs_path})",
                source_file,
                line_num,
            )
        self._manifest.included_files.append(abs_path)
        self._parse_file_impl(abs_path, scope)

    def _handle_subninja(
        self, rel_path: str, scope: dict[str, str], source_file: str, line_num: int
    ) -> None:
        """
        Handle 'subninja path' — parsed in a child scope.

        Variables from the subninja do NOT leak into the parent scope,
        but the parent's variables are visible as defaults.
        """
        abs_path = self._resolve_path(rel_path, source_file)
        if not os.path.isfile(abs_path):
            raise NinjaParserError(
                f"Subninja file not found: {rel_path} (resolved to {abs_path})",
                source_file,
                line_num,
            )
        self._manifest.included_files.append(abs_path)
        # Create a child scope that inherits parent variables
        child_scope = dict(scope)
        self._parse_file_impl(abs_path, child_scope)

    def _resolve_path(self, rel_path: str, source_file: str) -> str:
        """Resolve a path relative to the directory of the source file."""
        if os.path.isabs(rel_path):
            return rel_path
        # Resolve relative to the directory containing the current manifest
        base = os.path.dirname(source_file) if source_file != "<string>" else self._base_dir
        if not base:
            base = "."
        return os.path.normpath(os.path.join(base, rel_path))

    # ─────────────────────────────────────────────────────
    # Variable expansion
    # ─────────────────────────────────────────────────────

    def expand_variables(
        self,
        text: str,
        edge: Optional[BuildEdge] = None,
        rule: Optional[BuildRule] = None,
        scope: Optional[dict[str, str]] = None,
    ) -> str:
        """
        Expand $variable and ${variable} references in a string.

        Lookup order:
        1. Edge-specific variables
        2. Rule variables
        3. Top-level manifest variables
        4. Provided scope

        Special variables:
        - $in: space-joined explicit inputs
        - $out: space-joined explicit outputs
        - $$: literal '$'
        - $:: literal ':'
        """
        if "$" not in text:
            return text

        result: list[str] = []
        i = 0
        while i < len(text):
            if text[i] == "$":
                if i + 1 >= len(text):
                    result.append("$")
                    i += 1
                    continue

                next_ch = text[i + 1]

                # $$ -> literal $
                if next_ch == "$":
                    result.append("$")
                    i += 2
                    continue

                # $: -> literal :
                if next_ch == ":":
                    result.append(":")
                    i += 2
                    continue

                # $ (space) -> literal space
                if next_ch == " ":
                    result.append(" ")
                    i += 2
                    continue

                # ${variable}
                if next_ch == "{":
                    close = text.find("}", i + 2)
                    if close < 0:
                        result.append("${")
                        i += 2
                        continue
                    var_name = text[i + 2:close]
                    val = self._lookup_variable(var_name, edge, rule, scope)
                    result.append(val)
                    i = close + 1
                    continue

                # $variable (identifier: alphanumeric + underscore)
                j = i + 1
                while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                    j += 1
                if j > i + 1:
                    var_name = text[i + 1:j]
                    val = self._lookup_variable(var_name, edge, rule, scope)
                    result.append(val)
                    i = j
                    continue

                # Unrecognized $ sequence — keep literal
                result.append("$")
                i += 1
            else:
                result.append(text[i])
                i += 1

        return "".join(result)

    def _lookup_variable(
        self,
        name: str,
        edge: Optional[BuildEdge] = None,
        rule: Optional[BuildRule] = None,
        scope: Optional[dict[str, str]] = None,
    ) -> str:
        """
        Look up a variable by name in the correct precedence order.

        Special variables $in and $out are resolved from the edge.
        """
        # Special: $in and $out
        def _quote_if_needed(path_str: str) -> str:
            if " " in path_str and not (path_str.startswith('"') and path_str.endswith('"')):
                return f'"{path_str}"'
            return path_str

        if name == "in" and edge is not None:
            return " ".join(_quote_if_needed(x) for x in edge.explicit_inputs)
        if name == "in_newline" and edge is not None:
            return "\n".join(_quote_if_needed(x) for x in edge.explicit_inputs)
        if name == "out" and edge is not None:
            return " ".join(_quote_if_needed(x) for x in edge.outputs)

        # 1. Edge-specific variables
        if edge is not None and name in edge.variables:
            return edge.variables[name]

        # 2. Rule variables
        if rule is not None and name in rule.variables:
            return rule.variables[name]

        # 3. Top-level manifest variables
        if name in self._manifest.variables:
            return self._manifest.variables[name]

        # 4. Provided scope
        if scope is not None and name in scope:
            return scope[name]

        # Variable not found — return empty string (Ninja behaviour)
        return ""

    def resolve_edge_command(self, edge: BuildEdge) -> str:
        """
        Fully resolve the command for a build edge by expanding all variables.

        This is the command string that would be passed to the shell.
        """
        rule = self._manifest.rules.get(edge.rule_name)
        if not rule:
            return ""

        # Get the command template from the rule
        command_template = rule.command

        # First expand edge variables that might reference other edge variables
        # Then expand the command template with full context
        return self.expand_variables(command_template, edge=edge, rule=rule)

    def resolve_edge_description(self, edge: BuildEdge) -> str:
        """Resolve the description string for a build edge."""
        rule = self._manifest.rules.get(edge.rule_name)
        if not rule:
            return ""
        return self.expand_variables(rule.description, edge=edge, rule=rule)

    # ─────────────────────────────────────────────────────
    # Edge classification
    # ─────────────────────────────────────────────────────

    def _classify_edge(self, rule_name: str) -> EdgeType:
        """Classify a build edge based on its rule name."""
        if rule_name == "phony":
            return EdgeType.PHONY
        if rule_name == "RERUN_CMAKE":
            return EdgeType.RERUN_CMAKE
        if rule_name == "CLEAN":
            return EdgeType.CLEAN
        if rule_name == "HELP":
            return EdgeType.HELP
        if rule_name == "CUSTOM_COMMAND":
            return EdgeType.CUSTOM_COMMAND
        return EdgeType.BUILD

    # ─────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────

    @staticmethod
    def _is_valid_var_name(name: str) -> bool:
        """Check that a string looks like a valid Ninja variable name."""
        if not name:
            return False
        # Ninja variable names: [a-zA-Z0-9_.-]
        return all(c.isalnum() or c in ("_", ".", "-") for c in name)
