"""Software version and git metadata — computed once at import time.

VERSION is auto-derived from ``git describe --tags --always --dirty``, so
tagging a release (``git tag -a v1.2.3``) is the only step needed to bump the
version that appears in logs, the FastAPI banner, and per-recording metadata.
"""

from __future__ import annotations

import os
import re
import subprocess

# Matches the leading semver portion of a `git describe` output.
# Examples that match: "v1.1.3", "v1.1.3-2-g390fc9b", "v1.1.3-dirty",
# "v1.1.3-2-g390fc9b-dirty". A bare commit hash from `--always` does not match.
_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)")


def _git_info() -> dict:
    """Return git commit hash, dirty status, and describe string.

    Resolution order:
    1. ``git`` CLI (works in dev and anywhere ``.git/`` is present)
    2. ``GIT_DESCRIBE`` / ``GIT_COMMIT`` env vars (set at Docker build time)
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

    # Fallback: Docker build-time env vars.
    env_describe = os.environ.get("GIT_DESCRIBE", "").strip()
    env_commit = os.environ.get("GIT_COMMIT", "").strip()
    if env_commit:
        info["commit"] = env_commit
        info["commit_short"] = env_commit[:10]
    if env_describe:
        info["describe"] = env_describe
        info["dirty"] = env_describe.endswith("-dirty")
    elif env_commit:
        info["describe"] = env_commit[:10]

    return info


GIT_INFO = _git_info()


def _parse_version(describe: str) -> str:
    """Extract a ``MAJOR.MINOR.PATCH`` string from describe output."""
    match = _VERSION_RE.match(describe or "")
    return match.group(1) if match else "0.0.0"


VERSION = _parse_version(GIT_INFO["describe"])


def version_string() -> str:
    """Human-readable version derived from ``git describe``.

    Examples:
        ``v1.1.3``                  — exactly on tag, clean tree
        ``v1.1.3-dirty``            — exactly on tag, working tree dirty
        ``v1.1.3-2-g390fc9b``       — 2 commits past tag
        ``v1.1.3-2-g390fc9b-dirty`` — 2 commits past tag, dirty
        ``390fc9b``                 — no tags reachable; bare hash
    """
    desc = GIT_INFO.get("describe") or "unknown"
    if desc == "unknown":
        return f"unknown ({GIT_INFO.get('commit_short', 'unknown')})"
    return desc
