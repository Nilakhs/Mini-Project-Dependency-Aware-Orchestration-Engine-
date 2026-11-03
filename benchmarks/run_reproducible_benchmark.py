"""
Reproducible Multi-Trial Benchmark Runner

Executes empirical trials on examples/cpp_demo under identical clean conditions:
- Sequential Build (1 Worker): 3 Trials
- Parallel Build (4 Workers): 3 Trials
- No-op Build: 3 Trials
- Incremental Build (math.cpp modified): 3 Trials

Computes mean, standard deviation, speedup, parallel efficiency, and time saved.
Outputs formatted results and generates BENCHMARK_REPORT.md.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from api.main import clean_artifacts, DEFAULT_BASE_DIR, DEFAULT_MANIFEST, DEFAULT_DB
from engine.orchestrator import Orchestrator
from telemetry.db import TelemetryDB
from telemetry.report import calculate_comparison


async def run_single_trial(
    target: str,
    max_workers: int,
    db_path: str,
    clean: bool = True,
) -> dict[str, Any]:
    """Execute a single build trial and measure exact wall-clock duration."""
    if clean:
        clean_artifacts(DEFAULT_BASE_DIR, db_path=db_path)

    orch = Orchestrator(
        manifest_path=DEFAULT_MANIFEST,
        db_path=db_path,
        base_dir=DEFAULT_BASE_DIR,
        max_workers=max_workers,
    )

    t0 = time.perf_counter()
    res = await orch.build(target=target)
    t1 = time.perf_counter()
    wall_duration = t1 - t0
    orch.close()

    return {
        "success": res.success,
        "duration": wall_duration,
        "completed": res.completed_count,
        "skipped": res.skipped_count,
        "failed": res.failed_count,
        "blocked": res.blocked_count,
        "peak_concurrency": res.peak_concurrency,
    }


async def run_benchmark_suite(trials: int = 3) -> dict[str, Any]:
    print("=" * 70)
    print("   REPRODUCIBLE MULTI-TRIAL BENCHMARK SUITE")
    print(f"   Target Project: {DEFAULT_MANIFEST}")
    print(f"   Trials Per Scenario: {trials}")
    print("=" * 70)

    results = {
        "sequential": [],
        "parallel_4w": [],
        "noop": [],
        "incremental": [],
    }

    # 1. Sequential Trials (1 Worker)
    print(f"\n[1/4] Running {trials} Clean Sequential Trials (1 Worker)...")
    for i in range(1, trials + 1):
        db_trial = os.path.join(PROJECT_ROOT, "data", f"bench_trial_seq_{i}.db")
        data = await run_single_trial("all", max_workers=1, db_path=db_trial, clean=True)
        results["sequential"].append(data)
        print(f"  Trial {i}: {data['duration']:.3f} s (Peak concurrency: {data['peak_concurrency']}, Completed: {data['completed']})")
        if os.path.exists(db_trial):
            try: os.remove(db_trial)
            except OSError: pass

    # 2. Parallel Trials (4 Workers)
    print(f"\n[2/4] Running {trials} Clean Parallel Trials (4 Workers)...")
    for i in range(1, trials + 1):
        db_trial = os.path.join(PROJECT_ROOT, "data", f"bench_trial_par_{i}.db")
        data = await run_single_trial("all", max_workers=4, db_path=db_trial, clean=True)
        results["parallel_4w"].append(data)
        print(f"  Trial {i}: {data['duration']:.3f} s (Peak concurrency: {data['peak_concurrency']}, Completed: {data['completed']})")
        if os.path.exists(db_trial):
            try: os.remove(db_trial)
            except OSError: pass

    # 3. No-op Trials (Immediate Rebuild with 4 Workers)
    print(f"\n[3/4] Running {trials} No-op (Fully Cached) Trials...")
    db_noop = os.path.join(PROJECT_ROOT, "data", "bench_noop.db")
    # Initial build to populate cache
    await run_single_trial("all", max_workers=4, db_path=db_noop, clean=True)
    for i in range(1, trials + 1):
        data = await run_single_trial("all", max_workers=4, db_path=db_noop, clean=False)
        results["noop"].append(data)
        print(f"  Trial {i}: {data['duration']:.3f} s (Skipped: {data['skipped']}/{data['skipped'] + data['completed']})")

    # 4. Incremental Trials (modify math.cpp)
    print(f"\n[4/4] Running {trials} Incremental Rebuild Trials (math.cpp modified)...")
    math_cpp = os.path.join(PROJECT_ROOT, "examples", "cpp_demo", "src", "math.cpp")
    with open(math_cpp, "r", encoding="utf-8") as f:
        orig_code = f.read()

    try:
        for i in range(1, trials + 1):
            with open(math_cpp, "a", encoding="utf-8") as f:
                f.write(f"\n// benchmark iteration {i}\n")
            data = await run_single_trial("all", max_workers=4, db_path=db_noop, clean=False)
            results["incremental"].append(data)
            print(f"  Trial {i}: {data['duration']:.3f} s (Completed: {data['completed']}, Skipped: {data['skipped']})")
    finally:
        with open(math_cpp, "w", encoding="utf-8") as f:
            f.write(orig_code)
        if os.path.exists(db_noop):
            try: os.remove(db_noop)
            except OSError: pass

    # Compute Statistical Averages
    seq_durations = [t["duration"] for t in results["sequential"]]
    par_durations = [t["duration"] for t in results["parallel_4w"]]
    noop_durations = [t["duration"] for t in results["noop"]]
    inc_durations = [t["duration"] for t in results["incremental"]]

    mean_seq = statistics.mean(seq_durations)
    stdev_seq = statistics.stdev(seq_durations) if len(seq_durations) > 1 else 0.0

    mean_par = statistics.mean(par_durations)
    stdev_par = statistics.stdev(par_durations) if len(par_durations) > 1 else 0.0

    mean_noop = statistics.mean(noop_durations)
    mean_inc = statistics.mean(inc_durations)

    comp = calculate_comparison(
        target="all",
        sequential_duration=mean_seq,
        parallel_duration=mean_par,
        worker_count=4,
    )

    # Persist official benchmark into data/build_state.db
    db = TelemetryDB(DEFAULT_DB)
    db.record_comparison(comp.to_dict())
    db.close()

    print("\n" + "=" * 70)
    print("   FINAL STATISTICAL BENCHMARK SUMMARY")
    print("=" * 70)
    print(f"  Sequential (1w):   {mean_seq:.3f} ± {stdev_seq:.3f} s")
    print(f"  Parallel (4w):     {mean_par:.3f} ± {stdev_par:.3f} s")
    print(f"  Speedup Factor:    {comp.speedup:.2f}x")
    print(f"  Efficiency:        {comp.efficiency:.1f}%")
    print(f"  Time Saved:        {comp.time_saved:.3f} s ({comp.pct_reduction:.1f}% reduction)")
    print(f"  No-op Build:       {mean_noop:.3f} s ({mean_seq / max(0.001, mean_noop):.1f}x faster than clean)")
    print(f"  Incremental Build: {mean_inc:.3f} s")
    print("=" * 70)

    return {
        "sequential": results["sequential"],
        "parallel_4w": results["parallel_4w"],
        "noop": results["noop"],
        "incremental": results["incremental"],
        "mean_seq": mean_seq,
        "stdev_seq": stdev_seq,
        "mean_par": mean_par,
        "stdev_par": stdev_par,
        "mean_noop": mean_noop,
        "mean_inc": mean_inc,
        "comparison": comp.to_dict(),
    }


if __name__ == "__main__":
    asyncio.run(run_benchmark_suite(trials=3))
