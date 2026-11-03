"""
Benchmark Comparison & Reporting Engine

Computes and formats empirical performance metrics comparing sequential (1-worker)
and parallel (N-worker) build executions:
- Speedup: S_p = T_1 / T_p
- Parallel Efficiency: E_p = (S_p / p) * 100%
- Time Saved: Delta T = T_1 - T_p
- Percentage Reduction: ((T_1 - T_p) / T_1) * 100%
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class BenchmarkComparison:
    """Represents a measured sequential vs parallel build comparison."""
    comparison_id: str
    target: str
    sequential_duration: float
    parallel_duration: float
    worker_count: int
    speedup: float
    efficiency: float
    time_saved: float
    pct_reduction: float
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        lines = [
            "=" * 64,
            f"PERFORMANCE COMPARISON REPORT — Target: {self.target}",
            "=" * 64,
            f"  Sequential (1 Worker):   {self.sequential_duration:.3f} s",
            f"  Parallel ({self.worker_count} Workers):     {self.parallel_duration:.3f} s",
            f"  Speedup Factor:          {self.speedup:.2f}x",
            f"  Parallel Efficiency:     {self.efficiency:.1f}%",
            f"  Wall-Clock Time Saved:   {self.time_saved:.3f} s ({self.pct_reduction:.1f}% reduction)",
            "=" * 64,
        ]
        return "\n".join(lines)


def calculate_comparison(
    target: str,
    sequential_duration: float,
    parallel_duration: float,
    worker_count: int,
    comparison_id: Optional[str] = None,
) -> BenchmarkComparison:
    """
    Calculate speedup and parallel efficiency from empirical execution times.
    Safely handles edge cases like zero duration.
    """
    comp_id = comparison_id or f"cmp_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    
    t1 = max(0.0001, sequential_duration)
    tp = max(0.0001, parallel_duration)
    p = max(1, worker_count)

    speedup = t1 / tp
    efficiency = (speedup / p) * 100.0
    time_saved = max(0.0, sequential_duration - parallel_duration)
    pct_reduction = (time_saved / t1) * 100.0 if t1 > 0 else 0.0

    return BenchmarkComparison(
        comparison_id=comp_id,
        target=target,
        sequential_duration=round(sequential_duration, 4),
        parallel_duration=round(parallel_duration, 4),
        worker_count=p,
        speedup=round(speedup, 2),
        efficiency=round(efficiency, 1),
        time_saved=round(time_saved, 4),
        pct_reduction=round(pct_reduction, 1),
        timestamp=time.time(),
    )
