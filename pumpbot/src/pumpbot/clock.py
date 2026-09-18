"""Monotonic timing primitives for the hot path.

Everything latency-related in this codebase is measured in nanoseconds off
``time.perf_counter_ns``. Wall-clock time is recorded once per event, for
reports; it is never used for arithmetic, because NTP steps will happily tell
you a trade took -4 ms.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

NS_PER_MS = 1_000_000


def now_ns() -> int:
    return time.perf_counter_ns()


def wall_ms() -> float:
    return time.time() * 1000.0


@dataclass
class LatencyTrace:
    """Stage-by-stage timing for a single signal.

    A trade that loses money because we were 300 ms late looks exactly like a
    trade that loses money because the call was bad. The only way to tell them
    apart afterwards is to have measured. Every stage lands here.
    """

    trace_id: str
    stages: Dict[str, int] = field(default_factory=dict)
    _t0: int = field(default_factory=now_ns)
    _last: int = field(default_factory=now_ns)

    def mark(self, stage: str) -> None:
        t = now_ns()
        self.stages[stage] = t - self._last
        self._last = t

    @property
    def total_ns(self) -> int:
        return now_ns() - self._t0

    @property
    def total_ms(self) -> float:
        return self.total_ns / NS_PER_MS

    def as_ms(self) -> Dict[str, float]:
        return {k: v / NS_PER_MS for k, v in self.stages.items()}


class LatencyHistogram:
    """Cheap reservoir of samples; percentiles computed lazily at report time."""

    __slots__ = ("_samples", "_cap")

    def __init__(self, cap: int = 100_000) -> None:
        self._samples: List[float] = []
        self._cap = cap

    def add(self, ms: float) -> None:
        if len(self._samples) < self._cap:
            self._samples.append(ms)

    def percentile(self, p: float) -> Optional[float]:
        if not self._samples:
            return None
        s = sorted(self._samples)
        k = (len(s) - 1) * (p / 100.0)
        lo, hi = int(k), min(int(k) + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (k - lo)

    def summary(self) -> Dict[str, Optional[float]]:
        return {
            "count": len(self._samples),
            "p50": self.percentile(50),
            "p90": self.percentile(90),
            "p99": self.percentile(99),
            "max": max(self._samples) if self._samples else None,
        }


class ServerClock:
    """Tracks our offset from an exchange's server clock.

    Binance and friends reject orders whose ``timestamp`` falls outside
    ``recvWindow``. Under load, the naive fix is to widen the window; the
    correct fix is to know your offset and stop guessing.
    """

    __slots__ = ("offset_ms", "last_sync_ns", "rtt_ms")

    def __init__(self) -> None:
        self.offset_ms: float = 0.0
        self.last_sync_ns: int = 0
        self.rtt_ms: float = 0.0

    def observe(self, local_before_ms: float, server_ms: float, local_after_ms: float) -> None:
        self.rtt_ms = local_after_ms - local_before_ms
        midpoint = (local_before_ms + local_after_ms) / 2.0
        self.offset_ms = server_ms - midpoint
        self.last_sync_ns = now_ns()

    def timestamp_ms(self) -> int:
        return int(wall_ms() + self.offset_ms)

    def stale(self, max_age_s: float = 300.0) -> bool:
        if self.last_sync_ns == 0:
            return True
        return (now_ns() - self.last_sync_ns) / 1e9 > max_age_s
