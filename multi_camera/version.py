"""Software version and git metadata — computed once at import time."""

from __future__ import annotations

import os
import subprocess

VERSION = "1.0.0"


def _git_info() -> dict:
    """Return git commit hash, dirty status, and describe string.

    Resolution order:
    1. ``git`` CLI (works in dev and anywhere ``.git/`` is present)
    2. ``GIT_COMMIT`` environment variable (set at Docker build time)
    3. Falls back to ``"unknown"``
    """
    info = {"commit": "unknown", "commit_short": "unknown", "dirty": False, "describe": "unknown"}

    # Try git CLI first.  --always means describe won't fail even without tags.
    try:
        info["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
        info["commit_short"] = info["commit"][:10]
        info["dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True).strip())
        info["describe"] = subprocess.check_output(["git", "describe", "--tags", "--always", "--dirty"], stderr=subprocess.DEVNULL, text=True).strip()
        return info
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Fallback: Docker build-time env var.
    env_commit = os.environ.get("GIT_COMMIT", "")
    if env_commit:
        info["commit"] = env_commit
        info["commit_short"] = env_commit[:10]
        info["describe"] = env_commit[:10]

    return info


GIT_INFO = _git_info()


def version_string() -> str:
    """Human-readable version, e.g. ``'v1.0.0 (abc1234def)'`` or ``'v1.0.0 (abc1234def, dirty)'``."""
    dirty = ", dirty" if GIT_INFO["dirty"] else ""
    return f"v{VERSION} ({GIT_INFO['commit_short']}{dirty})"
