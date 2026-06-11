"""
Lightweight in-process performance counters for hotspots (agent-friendly).

Design goals:
- Zero external deps
- Very low overhead
- Safe for multi-threaded access
- Snapshot returns compact summary (count/avg/p50/p95/p99/max/last)
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import threading
from typing import Any, Deque, Dict


@dataclass(frozen=True)
class PerfStats:
    count: int
    avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    last_ms: float


class PerfMonitor:
    def __init__(self, max_samples_per_op: int = 1000):
        self._lock = threading.Lock()
        self._samples: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=max_samples_per_op))

    def record_ms(self, op: str, duration_ms: float) -> None:
        if not op:
            return
        # Guard against NaN/inf or negative noise
        try:
            if duration_ms < 0:
                return
            if duration_ms != duration_ms:  # NaN
                return
        except Exception:
            return
        with self._lock:
            self._samples[op].append(float(duration_ms))

    def reset(self) -> None:
        """Drop all recorded samples. Test-fixture helper; production code
        should rely on the per-op ring buffer's natural decay."""
        with self._lock:
            self._samples.clear()

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """
        Return compact stats per operation.
        """
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            items = list(self._samples.items())
        for op, samples in items:
            s = list(samples)
            if not s:
                continue
            s_sorted = sorted(s)
            n = len(s_sorted)

            def pct(p: float) -> float:
                if n == 1:
                    return s_sorted[0]
                # nearest-rank index
                idx = int(round((p / 100.0) * (n - 1)))
                idx = max(0, min(n - 1, idx))
                return s_sorted[idx]

            avg = sum(s_sorted) / n
            stats = PerfStats(
                count=n,
                avg_ms=avg,
                p50_ms=pct(50),
                p95_ms=pct(95),
                p99_ms=pct(99),
                max_ms=s_sorted[-1],
                last_ms=s[-1],
            )
            out[op] = {
                "count": stats.count,
                "avg_ms": round(stats.avg_ms, 3),
                "p50_ms": round(stats.p50_ms, 3),
                "p95_ms": round(stats.p95_ms, 3),
                "p99_ms": round(stats.p99_ms, 3),
                "max_ms": round(stats.max_ms, 3),
                "last_ms": round(stats.last_ms, 3),
                "sample_window": len(s_sorted),
            }
        return out


# Global singleton
perf_monitor = PerfMonitor()


def record_ms(op: str, duration_ms: float) -> None:
    perf_monitor.record_ms(op, duration_ms)


def snapshot() -> Dict[str, Dict[str, Any]]:
    return perf_monitor.snapshot()


