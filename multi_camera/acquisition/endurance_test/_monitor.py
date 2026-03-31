"""Extended pipeline monitor with thread, file descriptor, and SQLite tracking."""

from __future__ import annotations

import os
import threading
import time

from multi_camera.acquisition.stress_test._runner import PipelineMonitor


def _count_fds() -> int:
    """Count open file descriptors via /proc/self/fd (Linux only)."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return 0


class EnduranceMonitor(PipelineMonitor):
    """PipelineMonitor + thread count, FD count, and SQLite DB size tracking.

    Added overhead per sample: ~12us (thread count + readdir + stat).
    The inherited nvidia-smi subprocess call (~5ms) still dominates.
    """

    def __init__(self, interval_s: float = 60.0, db_path: str | None = None):
        super().__init__(interval_s=interval_s)
        self.db_path = db_path
        self.thread_count_samples: list[tuple[float, int]] = []
        self.fd_count_samples: list[tuple[float, int]] = []
        self.db_size_samples: list[tuple[float, int]] = []

    def _sample(self):
        super()._sample()
        elapsed = time.monotonic() - self._t0
        self.thread_count_samples.append((elapsed, threading.active_count()))
        self.fd_count_samples.append((elapsed, _count_fds()))
        if self.db_path:
            try:
                self.db_size_samples.append((elapsed, os.path.getsize(self.db_path)))
            except OSError:
                pass

    @staticmethod
    def _growth_per_hour(samples: list[tuple[float, float]], warmup_s: float = 120) -> float:
        """Linear growth rate after warmup, extrapolated to per-hour."""
        steady = [(t, v) for t, v in samples if t >= warmup_s]
        if len(steady) < 2:
            return 0.0
        first_t, first_v = steady[0]
        last_t, last_v = steady[-1]
        elapsed_h = (last_t - first_t) / 3600
        if elapsed_h < 0.01:
            return 0.0
        return (last_v - first_v) / elapsed_h

    @property
    def thread_count_growth_per_hour(self) -> float:
        return self._growth_per_hour(self.thread_count_samples)

    @property
    def fd_count_growth_per_hour(self) -> float:
        return self._growth_per_hour(self.fd_count_samples)

    @property
    def db_size_growth_kb_per_hour(self) -> float:
        return self._growth_per_hour(self.db_size_samples) / 1024
