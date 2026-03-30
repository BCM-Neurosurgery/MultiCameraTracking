"""Recording session logger: one log file per session, no terminal output (tqdm owns the terminal)."""

from __future__ import annotations

import logging
import os

from multi_camera.version import version_string


def setup_recording_logger(output_dir: str, session_name: str) -> logging.Logger:
    """Create a file-only logger at ``{output_dir}/{session_name}.log``.

    No StreamHandler — the tqdm progress bar owns stderr.  Workers use
    ``logging.getLogger("flir_pipeline")`` to write; the file handler
    persists everything for post-mortem inspection.
    """
    logger = logging.getLogger("flir_pipeline")
    logger.setLevel(logging.DEBUG)

    # Remove stale handlers from previous recording sessions so each
    # session gets its own log file with a fresh version header.
    for h in logger.handlers[:]:
        h.close()
        logger.removeHandler(h)

    log_path = os.path.join(output_dir, f"{session_name}.log")
    fh = logging.FileHandler(log_path)
    fh.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info("software: %s", version_string())
    return logger
