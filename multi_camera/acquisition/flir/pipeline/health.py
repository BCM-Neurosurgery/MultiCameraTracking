"""Thread-safe health counters for live progress bar display."""

from __future__ import annotations

import threading


class PipelineHealth:
    """Lightweight counters updated by pipeline workers, read by the tqdm progress bar.

    Integer reads/writes are atomic on CPython so ``format_status()`` never
    needs a lock — it just reads a few ints and formats a short string.
    """

    def __init__(self, num_cameras: int):
        self.num_cameras = num_cameras
        self._lock = threading.Lock()
        self.cameras_active = num_cameras
        self.dropped_frames = 0
        self.error_count = 0

    def inc_dropped(self):
        with self._lock:
            self.dropped_frames += 1

    def inc_errors(self):
        with self._lock:
            self.error_count += 1

    def format_status(self) -> str:
        """One-line status string for ``tqdm.set_postfix_str()``."""
        ok = self.error_count == 0 and self.dropped_frames == 0
        icon = "OK" if ok else "!!"
        return f"{icon} {self.cameras_active} cams | {self.dropped_frames} drops | {self.error_count} errs"
