"""
Tests for the Ninja manifest parser.

Tests are organized by feature:
1. Variable assignments
2. Variable expansion
3. Rule parsing
4. Build statement parsing (explicit, implicit, order-only inputs)
5. Phony targets
6. Line continuations
7. Ninja escape sequences ($:, $$, $ )
8. Include statements
9. Default targets
10. Unsupported rule detection
11. Real CMake-generated manifest parsing
"""

import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from engine.ninja_parser import NinjaParser, NinjaParserError
from engine.models import BuildEdge, BuildRule, EdgeType, NinjaManifest


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def parser():
    return NinjaParser()


# ─────────────────────────────────────────────────────────────
# 1. Variable assignments
# ─────────────────────────────────────────────────────────────

class TestVariableAssignments:
    def test_simple_variable(self, parser):
        manifest = parser.parse_string("ninja_required_version = 1.5\n")
        assert manifest.variables["ninja_required_version"] == "1.5"

    def test_variable_with_spaces_in_value(self, parser):
        manifest = parser.parse_string("builddir = build/output dir\n")
        assert manifest.variables["builddir"] == "build/output dir"

    def test_multiple_variables(self, parser):
        text = "var1 = hello\nvar2 = world\n"
        manifest = parser.parse_string(text)
        assert manifest.variables["var1"] == "hello"
        assert manifest.variables["var2"] == "world"

    def test_empty_value(self, parser):
        manifest = parser.parse_string("empty_var =\n")
        assert manifest.variables["empty_var"] == ""

    def test_cmake_workdir_variable(self, parser):
        """CMake generates a variable like: cmake_ninja_workdir = C$:/path/"""
        text = "cmake_ninja_workdir = C$:/Users/test/build/\n"
        manifest = parser.parse_string(text)
        assert manifest.variables["cmake_ninja_workdir"] == "C$:/Users/test/build/"


# ─────────────────────────────────────────────────────────────
# 2. Variable expansion
# ─────────────────────────────────────────────────────────────

class TestVariableExpansion:
    def test_simple_expansion(self, parser):
        parser.parse_string("greeting = hello\n")
        result = parser.expand_variables("$greeting world")
        assert result == "hello world"

    def test_braced_expansion(self, parser):
        parser.parse_string("LAUNCHER = \n")
        result = parser.expand_variables("${LAUNCHER}gcc")
        assert result == "gcc"

    def test_dollar_dollar(self, parser):
        parser.parse_string("")
        result = parser.expand_variables("cost is $$5")
        assert result == "cost is $5"

    def test_dollar_colon(self, parser):
        parser.parse_string("")
        result = parser.expand_variables("C$:/path")
        assert result == "C:/path"

    def test_dollar_space(self, parser):
        parser.parse_string("")
        result = parser.expand_variables("hello$ world")
        assert result == "hello world"

    def test_in_out_expansion(self, parser):
        parser.parse_string("")
        edge = BuildEdge(
            outputs=["out.o"],
            explicit_inputs=["src.cpp"],
            rule_name="compile",
        )
        result = parser.expand_variables("-o $out -c $in", edge=edge)
        assert result == "-o out.o -c src.cpp"

    def test_undefined_variable_is_empty(self, parser):
        parser.parse_string("")
        result = parser.expand_variables("$UNDEFINED_VAR")
        assert result == ""

    def test_edge_variables_override_rule(self, parser):
        parser.parse_string("")
        rule = BuildRule(name="test", variables={"FLAGS": "-O0"})
        edge = BuildEdge(
            outputs=["out.o"],
            rule_name="test",
            variables={"FLAGS": "-O2"},
        )
        result = parser.expand_variables("$FLAGS", edge=edge, rule=rule)
        assert result == "-O2"

    def test_rule_variables_used_when_edge_doesnt_override(self, parser):
        parser.parse_string("")
        rule = BuildRule(name="test", variables={"FLAGS": "-O0"})
        edge = BuildEdge(outputs=["out.o"], rule_name="test")
        result = parser.expand_variables("$FLAGS", edge=edge, rule=rule)
        assert result == "-O0"


# ─────────────────────────────────────────────────────────────
# 3. Rule parsing
# ─────────────────────────────────────────────────────────────

class TestRuleParsing:
    def test_simple_rule(self, parser):
        text = textwrap.dedent("""\
            rule compile
              command = gcc -c $in -o $out
              description = Compiling $in
        """)
        manifest = parser.parse_string(text)
        assert "compile" in manifest.rules
        rule = manifest.rules["compile"]
        assert rule.command == "gcc -c $in -o $out"
        assert rule.description == "Compiling $in"

    def test_rule_with_depfile(self, parser):
        text = textwrap.dedent("""\
            rule cxx_compile
              depfile = $DEP_FILE
              deps = gcc
              command = g++ -c $in -o $out
        """)
        manifest = parser.parse_string(text)
        rule = manifest.rules["cxx_compile"]
        assert rule.depfile == "$DEP_FILE"
        assert rule.deps == "gcc"

    def test_rule_with_restat(self, parser):
        text = textwrap.dedent("""\
            rule link
              command = g++ $in -o $out
              restat = $RESTAT
        """)
        manifest = parser.parse_string(text)
        rule = manifest.rules["link"]
        assert rule.restat == "$RESTAT"

    def test_rule_with_generator(self, parser):
        text = textwrap.dedent("""\
            rule RERUN_CMAKE
              command = cmake .
              generator = 1
        """)
        manifest = parser.parse_string(text)
        rule = manifest.rules["RERUN_CMAKE"]
        assert rule.generator is True

    def test_multiple_rules(self, parser):
        text = textwrap.dedent("""\
            rule compile
              command = gcc -c $in -o $out

            rule link
              command = gcc $in -o $out
        """)
        manifest = parser.parse_string(text)
        assert len(manifest.rules) == 2
        assert "compile" in manifest.rules
        assert "link" in manifest.rules


# ─────────────────────────────────────────────────────────────
# 4. Build statement parsing
# ─────────────────────────────────────────────────────────────

class TestBuildParsing:
    def test_simple_build(self, parser):
        text = textwrap.dedent("""\
            rule compile
              command = gcc -c $in -o $out

            build out.o: compile src.cpp
        """)
        manifest = parser.parse_string(text)
        assert len(manifest.edges) == 1
        edge = manifest.edges[0]
        assert edge.outputs == ["out.o"]
        assert edge.rule_name == "compile"
        assert edge.explicit_inputs == ["src.cpp"]

    def test_multiple_inputs(self, parser):
        text = "build out.exe: link a.o b.o c.o\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.explicit_inputs == ["a.o", "b.o", "c.o"]

    def test_multiple_outputs(self, parser):
        text = "build out1.o out2.o: compile src.cpp\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.outputs == ["out1.o", "out2.o"]

    def test_implicit_inputs(self, parser):
        """Inputs after | are implicit (tracked for rebuild but not in $in)."""
        text = "build demo.exe: link main.o | libmylib.a\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.explicit_inputs == ["main.o"]
        assert edge.implicit_inputs == ["libmylib.a"]

    def test_order_only_inputs(self, parser):
        """Inputs after || are order-only (must build first, but don't trigger rebuild)."""
        text = "build out.o: compile src.cpp || order_dep\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.explicit_inputs == ["src.cpp"]
        assert edge.order_only_inputs == ["order_dep"]

    def test_implicit_and_order_only(self, parser):
        """Test both | and || in the same build statement."""
        text = "build demo.exe: link main.o | libmylib.a || libmylib.a\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.explicit_inputs == ["main.o"]
        assert edge.implicit_inputs == ["libmylib.a"]
        assert edge.order_only_inputs == ["libmylib.a"]

    def test_edge_variables(self, parser):
        text = textwrap.dedent("""\
            build out.o: compile src.cpp
              DEP_FILE = out.o.d
              FLAGS = -O2
              OBJECT_DIR = CMakeFiles/dir
        """)
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.variables["DEP_FILE"] == "out.o.d"
        assert edge.variables["FLAGS"] == "-O2"
        assert edge.variables["OBJECT_DIR"] == "CMakeFiles/dir"

    def test_no_inputs(self, parser):
        """Build with no inputs (e.g., phony with no deps)."""
        text = "build clean: CLEAN\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.explicit_inputs == []

    def test_escaped_space_in_path(self, parser):
        """Ninja uses '$ ' for literal spaces in paths."""
        text = "build out.o: compile My$ Project/src.cpp\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.explicit_inputs == ["My Project/src.cpp"]

    def test_escaped_colon_in_path(self, parser):
        """CMake uses '$:' for literal colons in Windows paths."""
        text = "build out.o: compile C$:/Users/test/src.cpp\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.explicit_inputs == ["C:/Users/test/src.cpp"]


# ─────────────────────────────────────────────────────────────
# 5. Phony targets
# ─────────────────────────────────────────────────────────────

class TestPhonyTargets:
    def test_phony_alias(self, parser):
        text = "build all: phony hello.exe\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.is_phony
        assert edge.edge_type == EdgeType.PHONY
        assert edge.outputs == ["all"]
        assert edge.explicit_inputs == ["hello.exe"]

    def test_phony_with_order_only(self, parser):
        """CMake generates: build cmake_object_order_depends_target_X: phony || ."""
        text = "build cmake_object_order_depends_target_mylib: phony || .\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.is_phony
        assert edge.outputs == ["cmake_object_order_depends_target_mylib"]
        assert edge.order_only_inputs == ["."]

    def test_phony_no_inputs(self, parser):
        text = "build always_rebuild: phony\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.is_phony
        assert edge.explicit_inputs == []


# ─────────────────────────────────────────────────────────────
# 6. Line continuations
# ─────────────────────────────────────────────────────────────

class TestLineContinuations:
    def test_simple_continuation(self, parser):
        text = "build out.o: compile $\n  src.cpp\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.explicit_inputs == ["src.cpp"]

    def test_build_with_many_inputs_continued(self, parser):
        text = "build out.a: archive $\n  a.o $\n  b.o $\n  c.o\n"
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.explicit_inputs == ["a.o", "b.o", "c.o"]

    def test_dollar_dollar_not_continuation(self, parser):
        """'$$' at end of line is a literal $ — NOT a continuation."""
        text = "var = cost$$\nother = val\n"
        manifest = parser.parse_string(text)
        assert manifest.variables["var"] == "cost$$"
        assert manifest.variables["other"] == "val"


# ─────────────────────────────────────────────────────────────
# 7. Comments and blank lines
# ─────────────────────────────────────────────────────────────

class TestCommentsAndBlanks:
    def test_comments_ignored(self, parser):
        text = "# This is a comment\nvar = value\n# Another comment\n"
        manifest = parser.parse_string(text)
        assert manifest.variables["var"] == "value"
        assert len(manifest.variables) == 1

    def test_blank_lines_ignored(self, parser):
        text = "\n\nvar = value\n\n\n"
        manifest = parser.parse_string(text)
        assert manifest.variables["var"] == "value"


# ─────────────────────────────────────────────────────────────
# 8. Include statements
# ─────────────────────────────────────────────────────────────

class TestInclude:
    def test_include_file(self, parser):
        """Test that 'include' merges rules into the manifest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write the included file
            rules_path = os.path.join(tmpdir, "rules.ninja")
            with open(rules_path, "w") as f:
                f.write(textwrap.dedent("""\
                    rule compile
                      command = gcc -c $in -o $out
                """))

            # Write the main manifest
            main_path = os.path.join(tmpdir, "build.ninja")
            with open(main_path, "w") as f:
                f.write(textwrap.dedent("""\
                    include rules.ninja
                    build out.o: compile src.cpp
                """))

            manifest = parser.parse_file(main_path)
            assert "compile" in manifest.rules
            assert len(manifest.edges) == 1
            assert rules_path in manifest.included_files

    def test_include_nested(self, parser):
        """Test include within include."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Deeply nested file
            os.makedirs(os.path.join(tmpdir, "sub"))
            with open(os.path.join(tmpdir, "sub", "deep.ninja"), "w") as f:
                f.write("deep_var = deep_value\n")

            with open(os.path.join(tmpdir, "rules.ninja"), "w") as f:
                f.write("include sub/deep.ninja\nrule compile\n  command = gcc\n")

            with open(os.path.join(tmpdir, "build.ninja"), "w") as f:
                f.write("include rules.ninja\n")

            manifest = parser.parse_file(os.path.join(tmpdir, "build.ninja"))
            assert manifest.variables["deep_var"] == "deep_value"
            assert "compile" in manifest.rules


# ─────────────────────────────────────────────────────────────
# 9. Default targets
# ─────────────────────────────────────────────────────────────

class TestDefaults:
    def test_default_target(self, parser):
        text = "default all\n"
        manifest = parser.parse_string(text)
        assert manifest.defaults == ["all"]

    def test_multiple_defaults(self, parser):
        text = "default target1 target2\n"
        manifest = parser.parse_string(text)
        assert "target1" in manifest.defaults
        assert "target2" in manifest.defaults


# ─────────────────────────────────────────────────────────────
# 10. Unsupported rule detection
# ─────────────────────────────────────────────────────────────

class TestUnsupportedRules:
    def test_rerun_cmake_detected(self, parser):
        text = textwrap.dedent("""\
            rule RERUN_CMAKE
              command = cmake .
              generator = 1

            build build.ninja: RERUN_CMAKE | CMakeLists.txt
        """)
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        assert edge.edge_type == EdgeType.RERUN_CMAKE

        # Should have a warning
        rerun_warnings = [w for w in manifest.warnings if "RERUN_CMAKE" in w]
        assert len(rerun_warnings) > 0, "Expected warning about unsupported RERUN_CMAKE rule"

    def test_clean_detected(self, parser):
        text = "build clean: CLEAN\n"
        manifest = parser.parse_string(text)
        assert manifest.edges[0].edge_type == EdgeType.CLEAN

    def test_help_detected(self, parser):
        text = "build help: HELP\n"
        manifest = parser.parse_string(text)
        assert manifest.edges[0].edge_type == EdgeType.HELP

    def test_unsupported_edges_listed(self, parser):
        text = textwrap.dedent("""\
            rule RERUN_CMAKE
              command = cmake .
            rule CLEAN
              command = ninja -t clean
            rule HELP
              command = ninja -t targets

            build build.ninja: RERUN_CMAKE
            build clean: CLEAN
            build help: HELP
            build out.o: phony
        """)
        manifest = parser.parse_string(text)
        unsupported = manifest.get_unsupported_edges()
        assert len(unsupported) == 3

    def test_pool_warning(self, parser):
        text = textwrap.dedent("""\
            pool console
              depth = 1
        """)
        manifest = parser.parse_string(text)
        pool_warnings = [w for w in manifest.warnings if "console" in w]
        assert len(pool_warnings) > 0


# ─────────────────────────────────────────────────────────────
# 11. Manifest summary
# ─────────────────────────────────────────────────────────────

class TestManifestSummary:
    def test_summary_contains_counts(self, parser):
        text = textwrap.dedent("""\
            rule compile
              command = gcc $in -o $out

            build a.o: compile a.cpp
            build b.o: compile b.cpp
            build all: phony a.o b.o
            default all
        """)
        manifest = parser.parse_string(text)
        summary = manifest.summary()
        assert "Rules: 1" in summary
        assert "Build edges: 3" in summary
        assert "Normal build: 2" in summary
        assert "Phony aliases: 1" in summary


# ─────────────────────────────────────────────────────────────
# 12. Command resolution
# ─────────────────────────────────────────────────────────────

class TestCommandResolution:
    def test_resolve_simple_command(self, parser):
        text = textwrap.dedent("""\
            rule compile
              command = gcc -c $in -o $out

            build out.o: compile src.cpp
        """)
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        cmd = parser.resolve_edge_command(edge)
        assert cmd == "gcc -c src.cpp -o out.o"

    def test_resolve_with_edge_variables(self, parser):
        text = textwrap.dedent("""\
            rule compile
              command = gcc $FLAGS -c $in -o $out

            build out.o: compile src.cpp
              FLAGS = -O2 -Wall
        """)
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        cmd = parser.resolve_edge_command(edge)
        assert cmd == "gcc -O2 -Wall -c src.cpp -o out.o"

    def test_resolve_with_braced_variables(self, parser):
        """CMake uses ${LAUNCHER}${CODE_CHECK} which are typically empty."""
        text = textwrap.dedent("""\
            rule compile
              command = ${LAUNCHER}${CODE_CHECK}gcc -c $in -o $out

            build out.o: compile src.cpp
        """)
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        cmd = parser.resolve_edge_command(edge)
        assert cmd == "gcc -c src.cpp -o out.o"

    def test_resolve_description(self, parser):
        text = textwrap.dedent("""\
            rule compile
              command = gcc -c $in -o $out
              description = Building CXX object $out

            build out.o: compile src.cpp
        """)
        manifest = parser.parse_string(text)
        edge = manifest.edges[0]
        desc = parser.resolve_edge_description(edge)
        assert desc == "Building CXX object out.o"


# ─────────────────────────────────────────────────────────────
# 13. Real CMake-generated manifest
# ─────────────────────────────────────────────────────────────

class TestRealManifest:
    """
    These tests parse the actual CMake-generated build.ninja from our
    examples/cpp_demo project. They are skipped if the file doesn't exist.
    """

    @pytest.fixture
    def manifest_path(self):
        path = os.path.join(
            PROJECT_ROOT, "examples", "cpp_demo", "build", "build.ninja"
        )
        if not os.path.isfile(path):
            pytest.skip("CMake-generated build.ninja not found. Run cmake first.")
        return path

    def test_parses_without_error(self, parser, manifest_path):
        """The parser should handle the real manifest without raising exceptions."""
        manifest = parser.parse_file(manifest_path)
        assert manifest is not None

    def test_rules_found(self, parser, manifest_path):
        """Should find CXX compiler and linker rules."""
        manifest = parser.parse_file(manifest_path)
        rule_names = set(manifest.rules.keys())

        # Expect at least compiler and linker rules for our targets
        assert any("CXX_COMPILER" in name for name in rule_names), \
            f"Expected CXX compiler rule, found: {rule_names}"
        assert any("LINKER" in name for name in rule_names), \
            f"Expected linker rule, found: {rule_names}"

    def test_build_edges_found(self, parser, manifest_path):
        """Should find build edges for our 4 source files + 2 link steps + 1 archive."""
        manifest = parser.parse_file(manifest_path)
        build_edges = manifest.get_build_edges()

        # We expect at least:
        # 4 compile edges (math.cpp, utils.cpp, stringops.cpp, fileio.cpp) for mylib
        # 1 compile edge (main.cpp) for demo
        # 1 compile edge (test_runner.cpp) for test_runner
        # 1 static library link (libmylib.a)
        # 1 executable link (demo.exe)
        # 1 executable link (test_runner.exe)
        # Plus possibly CUSTOM_COMMAND edges for edit_cache, rebuild_cache
        assert len(build_edges) >= 9, \
            f"Expected >= 9 build edges, got {len(build_edges)}: " \
            f"{[e.primary_output for e in build_edges]}"

    def test_phony_targets_found(self, parser, manifest_path):
        """Should find phony aliases (all, demo, mylib, test_runner, etc.)."""
        manifest = parser.parse_file(manifest_path)
        phony_edges = manifest.get_phony_edges()
        phony_outputs = set()
        for e in phony_edges:
            phony_outputs.update(e.outputs)

        assert "all" in phony_outputs, f"Expected 'all' phony target, got: {phony_outputs}"
        assert "demo" in phony_outputs, f"Expected 'demo' phony target, got: {phony_outputs}"

    def test_default_target(self, parser, manifest_path):
        manifest = parser.parse_file(manifest_path)
        assert "all" in manifest.defaults, f"Expected default 'all', got: {manifest.defaults}"

    def test_unsupported_edges_detected(self, parser, manifest_path):
        """RERUN_CMAKE, CLEAN, HELP should be detected and flagged."""
        manifest = parser.parse_file(manifest_path)
        unsupported = manifest.get_unsupported_edges()
        unsupported_rules = {e.rule_name for e in unsupported}

        assert "RERUN_CMAKE" in unsupported_rules, \
            f"Expected RERUN_CMAKE in unsupported edges. Got: {unsupported_rules}"
        assert "CLEAN" in unsupported_rules
        assert "HELP" in unsupported_rules

    def test_include_rules_ninja(self, parser, manifest_path):
        """Should have processed the included CMakeFiles/rules.ninja."""
        manifest = parser.parse_file(manifest_path)
        assert len(manifest.included_files) >= 1, "Expected at least one included file"
        assert any("rules.ninja" in f for f in manifest.included_files)

    def test_compile_edges_have_correct_structure(self, parser, manifest_path):
        """Check that compile edges have the right inputs/outputs/variables."""
        manifest = parser.parse_file(manifest_path)
        build_edges = manifest.get_build_edges()

        # Find a compile edge
        compile_edges = [e for e in build_edges
                         if "CXX_COMPILER" in e.rule_name]
        assert len(compile_edges) >= 4, \
            f"Expected >= 4 compile edges, got {len(compile_edges)}"

        for edge in compile_edges:
            # Each compile edge should have exactly one output (.obj)
            assert len(edge.outputs) == 1, f"Expected 1 output, got {edge.outputs}"
            assert edge.outputs[0].endswith(".obj"), f"Expected .obj output: {edge.outputs[0]}"

            # Should have exactly one explicit input (.cpp)
            assert len(edge.explicit_inputs) == 1, \
                f"Expected 1 explicit input, got {edge.explicit_inputs}"
            assert edge.explicit_inputs[0].endswith(".cpp"), \
                f"Expected .cpp input: {edge.explicit_inputs[0]}"

            # Should have edge variables (DEP_FILE, FLAGS, etc.)
            assert "DEP_FILE" in edge.variables
            assert "FLAGS" in edge.variables

    def test_link_edges_have_implicit_deps(self, parser, manifest_path):
        """Link edges for executables should have implicit dep on libmylib.a."""
        manifest = parser.parse_file(manifest_path)
        build_edges = manifest.get_build_edges()

        exe_link_edges = [e for e in build_edges
                          if "EXECUTABLE_LINKER" in e.rule_name]
        assert len(exe_link_edges) == 2, \
            f"Expected 2 executable link edges, got {len(exe_link_edges)}"

        for edge in exe_link_edges:
            # Should have implicit input on libmylib.a
            assert "libmylib.a" in edge.implicit_inputs, \
                f"Expected libmylib.a in implicit inputs: {edge.implicit_inputs}"

    def test_command_resolution_for_compile(self, parser, manifest_path):
        """Resolved command should be a real g++ invocation."""
        manifest = parser.parse_file(manifest_path)
        build_edges = manifest.get_build_edges()

        compile_edges = [e for e in build_edges
                         if "CXX_COMPILER" in e.rule_name]
        assert len(compile_edges) > 0

        edge = compile_edges[0]
        cmd = parser.resolve_edge_command(edge)

        # The command should contain the compiler path and basic flags
        assert "c++.exe" in cmd or "g++" in cmd, f"Expected compiler in command: {cmd}"
        assert "-c" in cmd, f"Expected -c flag in command: {cmd}"
        assert "-o" in cmd, f"Expected -o flag in command: {cmd}"

    def test_escaped_paths_decoded(self, parser, manifest_path):
        """CMake generates paths with $: and $ escapes — verify they're decoded."""
        manifest = parser.parse_file(manifest_path)
        build_edges = manifest.get_build_edges()

        compile_edges = [e for e in build_edges
                         if "CXX_COMPILER" in e.rule_name]

        for edge in compile_edges:
            for inp in edge.explicit_inputs:
                # Decoded paths should not contain '$:' or '$ '
                assert "$:" not in inp, f"Undecoded '$:' in input path: {inp}"
                assert "$ " not in inp, f"Undecoded '$ ' in input path: {inp}"
                # Should contain normal colon in drive letter
                assert ":" in inp or "/" in inp, f"Expected path separators in: {inp}"

    def test_manifest_summary(self, parser, manifest_path):
        """Summary should print without errors and contain meaningful data."""
        manifest = parser.parse_file(manifest_path)
        summary = manifest.summary()
        assert "Rules:" in summary
        assert "Build edges:" in summary
        print("\n" + summary)

    def test_warnings_are_informative(self, parser, manifest_path):
        """All warnings should contain enough context to understand the issue."""
        manifest = parser.parse_file(manifest_path)
        for warning in manifest.warnings:
            # Each warning should mention the rule or construct
            assert len(warning) > 20, f"Warning too short to be useful: {warning}"
        # Print warnings for review
        if manifest.warnings:
            print("\nParser warnings:")
            for w in manifest.warnings:
                print(f"  - {w}")
