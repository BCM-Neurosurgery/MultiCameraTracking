"""Bridge Spinnaker SDK log events into the ``flir_pipeline`` Python logger.

Spinnaker's ``LoggingEventHandler`` callback fires from internal SDK threads
when the system logs at or above the configured priority. Without a bridge
the messages go nowhere visible to us; with it, they land in the same
per-session ``<session>.log`` file used by the rest of the pipeline.

Usage:

    handler = spinnaker_logging.attach(system, level="WARN")
    ...
    spinnaker_logging.detach(system, handler)   # before ReleaseInstance()

Level can be one of: OFF, FATAL, ERROR, WARN, NOTICE, INFO, DEBUG, TRACE.
The PySpin constant set varies by SDK version, so unrecognized levels fall
back to the next strictest.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("flir_pipeline")

try:
    import PySpin  # type: ignore

    _HAS_PYSPIN = True
except Exception:  # pragma: no cover
    PySpin = None  # type: ignore
    _HAS_PYSPIN = False


_NAME_TO_LEVEL = {
    "OFF": logging.NOTSET,
    "FATAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "NOTICE": logging.INFO,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "TRACE": logging.DEBUG,
}


def _resolve_spinnaker_level(level_name: str) -> int:
    if not _HAS_PYSPIN:
        return 0
    name = (level_name or "WARN").upper()
    fallbacks = {
        "OFF": ("SPINNAKER_LOG_LEVEL_OFF",),
        "FATAL": ("SPINNAKER_LOG_LEVEL_FATAL", "SPINNAKER_LOG_LEVEL_ERROR"),
        "ERROR": ("SPINNAKER_LOG_LEVEL_ERROR",),
        "WARN": ("SPINNAKER_LOG_LEVEL_WARN", "SPINNAKER_LOG_LEVEL_ERROR"),
        "WARNING": ("SPINNAKER_LOG_LEVEL_WARN", "SPINNAKER_LOG_LEVEL_ERROR"),
        "NOTICE": ("SPINNAKER_LOG_LEVEL_NOTICE", "SPINNAKER_LOG_LEVEL_INFO"),
        "INFO": ("SPINNAKER_LOG_LEVEL_INFO", "SPINNAKER_LOG_LEVEL_NOTICE"),
        "DEBUG": ("SPINNAKER_LOG_LEVEL_DEBUG",),
        "TRACE": ("SPINNAKER_LOG_LEVEL_TRACE", "SPINNAKER_LOG_LEVEL_DEBUG"),
    }
    for attr in fallbacks.get(name, ("SPINNAKER_LOG_LEVEL_WARN",)):
        if hasattr(PySpin, attr):
            return getattr(PySpin, attr)
    return getattr(PySpin, "SPINNAKER_LOG_LEVEL_WARN", 0)


if _HAS_PYSPIN:

    class SpinnakerLogBridge(PySpin.LoggingEventHandler):  # type: ignore[misc]
        def __init__(self):
            super().__init__()

        def OnLogEvent(self, logging_event_data) -> None:
            try:
                priority = logging_event_data.GetPriorityName()
            except Exception:
                priority = "INFO"
            level = _NAME_TO_LEVEL.get(str(priority).upper(), logging.INFO)
            try:
                category = logging_event_data.GetCategoryName()
            except Exception:
                category = "?"
            try:
                message = logging_event_data.GetLogMessage()
            except Exception:
                message = "<no message>"
            log.log(level, "SpinSDK[%s/%s] %s", category, priority, message)

else:

    class SpinnakerLogBridge:  # type: ignore[no-redef]
        pass


def attach(system, level: str = "WARN") -> Optional["SpinnakerLogBridge"]:
    if not _HAS_PYSPIN or system is None:
        return None
    try:
        handler = SpinnakerLogBridge()
        system.RegisterLoggingEventHandler(handler)
        system.SetLoggingEventPriorityLevel(_resolve_spinnaker_level(level))
        log.info("Spinnaker SDK log bridge attached at level=%s", level)
        return handler
    except Exception as exc:
        log.warning("Spinnaker SDK log bridge attach failed: %s", exc)
        return None


def detach(system, handler: Optional["SpinnakerLogBridge"]) -> None:
    if not _HAS_PYSPIN or system is None or handler is None:
        return
    try:
        system.UnregisterLoggingEventHandler(handler)
        log.info("Spinnaker SDK log bridge detached")
    except Exception as exc:
        log.warning("Spinnaker SDK log bridge detach failed: %s", exc)
