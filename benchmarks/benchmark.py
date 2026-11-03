"""
Performance Benchmarks & Real Project Demonstrations.

Executes and measures on examples/cpp_demo:
1. Clean sequential build (--jobs 1)
2. Clean parallel build (--jobs 4)
3. No-op build (all 9 tasks skipped)
4. Incremental build after modifying math.cpp
5. Build failure & downstream blocking
6. Worker limit concurrency verification
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from engine.orchestrator import Orchestrator
from engine.scheduler import TaskStatus

MANIFEST = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "build", "build.ninja")
BASE_DIR = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "build")


def clean_build_artifacts():
    """Remove built binaries and object files from build dir to ensure clean state."""
    patterns = [
        "demo.exe",
        "test_runner.exe",
        "libmylib.a",
        "CMakeFiles/mylib.dir/src/math.cpp.obj",
        "CMakeFiles/mylib.dir/src/utils.cpp.obj",
        "CMakeFiles/mylib.dir/src/stringops.cpp.obj",
        "CMakeFiles/mylib.dir/src/fileio.cpp.obj",
        "CMakeFiles/demo.dir/src/main.cpp.obj",
        "CMakeFiles/test_runner.dir/src/test_runner.cpp.obj",
    ]
    for p in patterns:
        full_p = os.path.join(BASE_DIR, p)
        if os.path.isfile(full_p):
            try:
                os.remove(full_p)
            except OSError:
                pass


async def run_demonstration():
    print("=" * 70)
    print("   DEPENDENCY-AWARE TASK ORCHESTRATION ENGINE — BENCHMARK SUITE")
    print("=" * 70)
    print(f"Project:  {MANIFEST}")
    print(f"Base Dir: {BASE_DIR}")
    print()

    with tempfile.TemporaryDirectory() as temp_dir:
        # ─────────────────────────────────────────────────────────────
        # 1. Clean Sequential Build (--jobs 1)
        # ─────────────────────────────────────────────────────────────
        print("-" * 70)
        print("DEMO 1: Clean Sequential Build (--jobs 1)")
        print("-" * 70)
        clean_build_artifacts()
        db_seq = os.path.join(temp_dir, "seq_build.db")
        orch_seq = Orchestrator(MANIFEST, db_path=db_seq, base_dir=BASE_DIR, max_workers=1)

        t0 = time.time()
        res_seq = await orch_seq.build(target="all")
        t_seq = time.time() - t0
        orch_seq.close()

        print(f"  Duration:         {t_seq:.3f} s")
        print(f"  Completed Tasks:  {res_seq.completed_count}/{res_seq.total_tasks}")
        print(f"  Peak Concurrency: {res_seq.peak_concurrency} worker")
        print(f"  Status:           {'SUCCESS' if res_seq.success else 'FAILED'}")

        # ─────────────────────────────────────────────────────────────
        # 2. Clean Parallel Build (--jobs 4)
        # ─────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("DEMO 2: Clean Parallel Build (--jobs 4)")
        print("-" * 70)
        clean_build_artifacts()
        db_par = os.path.join(temp_dir, "par_build.db")
        orch_par = Orchestrator(MANIFEST, db_path=db_par, base_dir=BASE_DIR, max_workers=4)

        t0 = time.time()
        res_par = await orch_par.build(target="all")
        t_par = time.time() - t0

        speedup = t_seq / t_par if t_par > 0 else 1.0
        efficiency = (speedup / 4.0) * 100.0

        print(f"  Duration:         {t_par:.3f} s")
        print(f"  Completed Tasks:  {res_par.completed_count}/{res_par.total_tasks}")
        print(f"  Peak Concurrency: {res_par.peak_concurrency} workers")
        print(f"  Speedup:          {speedup:.2f}x")
        print(f"  Efficiency:       {efficiency:.1f}%")
        print(f"  Status:           {'SUCCESS' if res_par.success else 'FAILED'}")

        # ─────────────────────────────────────────────────────────────
        # 3. No-Op Build (Zero modifications)
        # ─────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("DEMO 3: No-Op Build (Identical run without changes)")
        print("-" * 70)
        t0 = time.time()
        res_noop = await orch_par.build(target="all")
        t_noop = time.time() - t0

        print(f"  Duration:         {t_noop:.3f} s (vs {t_par:.3f} s clean)")
        print(f"  Completed:        {res_noop.completed_count}")
        print(f"  Skipped:          {res_noop.skipped_count}/{res_noop.total_tasks}")
        print(f"  Status:           {'SUCCESS' if res_noop.success else 'FAILED'}")

        # ─────────────────────────────────────────────────────────────
        # 4. Incremental Build (Modify math.cpp)
        # ─────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("DEMO 4: Incremental Build (Touch math.cpp)")
        print("-" * 70)
        math_cpp = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "src", "math.cpp")
        with open(math_cpp, "rb") as f:
            orig_math = f.read()

        try:
            with open(math_cpp, "ab") as f:
                f.write(b"\n// Bench modification\n")

            t0 = time.time()
            res_inc = await orch_par.build(target="all")
            t_inc = time.time() - t0

            print(f"  Duration:         {t_inc:.3f} s")
            print(f"  Completed:        {res_inc.completed_count} (only affected tasks)")
            print(f"  Skipped:          {res_inc.skipped_count} (unaffected tasks)")
            for tid, rec in res_inc.task_records.items():
                print(f"    [{rec.status.value:<9}] {tid}")

        finally:
            with open(math_cpp, "wb") as f:
                f.write(orig_math)

        # ─────────────────────────────────────────────────────────────
        # 5. Build Failure & Downstream Blocking
        # ─────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("DEMO 5: Failure Propagation & Downstream Blocking")
        print("-" * 70)
        # Break math.cpp with a syntax error
        try:
            with open(math_cpp, "wb") as f:
                f.write(b"#error SYNTAX_ERROR_INDUCED_FOR_TEST\n" + orig_math)

            res_fail = await orch_par.build(target="all")
            print(f"  Status:           {'SUCCESS' if res_fail.success else 'FAILED'}")
            print(f"  Failed Tasks:     {res_fail.failed_count}")
            print(f"  Blocked Tasks:    {res_fail.blocked_count}")
            print(f"  Skipped Tasks:    {res_fail.skipped_count}")

            for tid, rec in res_fail.task_records.items():
                if rec.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
                    print(f"    [{rec.status.value:<9}] {tid}")

        finally:
            with open(math_cpp, "wb") as f:
                f.write(orig_math)

        # ─────────────────────────────────────────────────────────────
        # 6. Worker Scaling Comparison (1 vs 2 vs 4 workers)
        # ─────────────────────────────────────────────────────────────
        print("\n" + "-" * 70)
        print("DEMO 6: Worker Scaling Benchmark (Clean Builds)")
        print("-" * 70)
        worker_counts = [1, 2, 4]
        scaling_results = {}

        for w in worker_counts:
            clean_build_artifacts()
            db_scale = os.path.join(temp_dir, f"scale_{w}.db")
            orch_scale = Orchestrator(MANIFEST, db_path=db_scale, base_dir=BASE_DIR, max_workers=w)
            t0 = time.time()
            res_scale = await orch_scale.build(target="all")
            dt = time.time() - t0
            orch_scale.close()
            scaling_results[w] = (dt, res_scale.peak_concurrency)
            print(f"  Workers: {w} -> Duration: {dt:.3f} s (Peak Concurrency: {res_scale.peak_concurrency})")

        orch_par.close()

    print("\n" + "=" * 70)
    print("   BENCHMARK DEMONSTRATION COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_demonstration())
