"""Per-camera Spinnaker TLStream nodemap telemetry, polled on the capture loop.

Resolves a fixed set of stream-buffer counter handles once per camera at
construction. The hot-path poll only calls ``.GetValue()`` on cached
``CIntegerPtr`` handles, so periodic ticks add a single nodemap read per
camera per cycle. Counters not present on a given SDK / firmware version
are recorded once at baseline as MISSING and never retried.

Output format (single line per camera, grep-friendly):

    StreamStats <serial> reason=<reason> in=N out=N ann=N started=N drop=N lost=N incomp=N failed=N

Use ``log_baseline()`` once at session start, ``log_snapshot(sn, reason)``
on a periodic tick or at first per-camera timeout, and again at watchdog
shutdown.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Mapping, Optional

log = logging.getLogger("flir_pipeline")

_COUNTER_NAMES: tuple[str, ...] = (
    "StreamInputBufferCount",
    "StreamOutputBufferCount",
    "StreamAnnouncedBufferCount",
    "StreamStartedFrameCount",
    "StreamDroppedFrameCount",
    "StreamLostFrameCount",
    "StreamIncompleteFrameCount",
    "StreamFailedBufferCount",
)

_SHORT: Mapping[str, str] = {
    "StreamInputBufferCount": "in",
    "StreamOutputBufferCount": "out",
    "StreamAnnouncedBufferCount": "ann",
    "StreamStartedFrameCount": "started",
    "StreamDroppedFrameCount": "drop",
    "StreamLostFrameCount": "lost",
    "StreamIncompleteFrameCount": "incomp",
    "StreamFailedBufferCount": "failed",
}


def _default_int_ptr(node):
    import PySpin

    return PySpin.CIntegerPtr(node)


def _default_is_readable(ptr) -> bool:
    import PySpin

    return PySpin.IsReadable(ptr)


class StreamTelemetry:
    def __init__(
        self,
        cameras: Iterable,
        serial_map: Mapping,
        int_ptr_factory: Callable = _default_int_ptr,
        is_readable: Callable = _default_is_readable,
    ):
        self._handles: dict[str, dict[str, object]] = {}
        self._missing: dict[str, list[str]] = {}
        for camera in cameras:
            sn = serial_map[id(camera)]
            self._handles[sn] = {}
            self._missing[sn] = []
            # simple_pyspin.Camera wraps the raw PySpin camera as .cam and only forwards
            # GenICam node attributes via __getattr__; methods like GetTLStreamNodeMap
            # must be called on the inner camera. Fall back to the camera itself for
            # raw-PySpin or test-mock instances.
            raw = getattr(camera, "cam", camera)
            try:
                nodemap = raw.GetTLStreamNodeMap()
            except Exception as exc:
                log.warning("StreamTelemetry %s: GetTLStreamNodeMap failed (%s)", sn, exc)
                continue
            for name in _COUNTER_NAMES:
                try:
                    node = nodemap.GetNode(name)
                    if node is None:
                        self._missing[sn].append(name)
                        continue
                    ptr = int_ptr_factory(node)
                    if not is_readable(ptr):
                        self._missing[sn].append(name)
                        continue
                    self._handles[sn][name] = ptr
                except Exception as exc:
                    log.debug("StreamTelemetry %s/%s: resolve failed (%s)", sn, name, exc)
                    self._missing[sn].append(name)

    def serials(self) -> list[str]:
        return list(self._handles.keys())

    def poll(self, serial: str) -> dict[str, Optional[int]]:
        out: dict[str, Optional[int]] = {}
        for name, ptr in self._handles.get(serial, {}).items():
            try:
                out[name] = int(ptr.GetValue())
            except Exception as exc:
                log.debug("StreamTelemetry %s/%s: read failed (%s)", serial, name, exc)
                out[name] = None
        return out

    def log_baseline(self) -> None:
        for sn in self._handles:
            stats = self.poll(sn)
            log.info("StreamStats %s reason=baseline %s", sn, _format(stats))
            missing = self._missing.get(sn, [])
            if missing:
                log.info("StreamStats %s missing=%s", sn, ",".join(missing))

    def log_snapshot(self, serial: str, reason: str, level: int = logging.WARNING) -> None:
        if serial not in self._handles:
            return
        stats = self.poll(serial)
        log.log(level, "StreamStats %s reason=%s %s", serial, reason, _format(stats))


def _format(stats: Mapping[str, Optional[int]]) -> str:
    parts = []
    for name in _COUNTER_NAMES:
        if name in stats:
            v = stats[name]
            parts.append(f"{_SHORT[name]}={'?' if v is None else v}")
    return " ".join(parts)
