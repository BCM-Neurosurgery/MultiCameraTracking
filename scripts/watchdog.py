#!/usr/bin/env python3
"""Recording watchdog — monitors host health and alerts Slack.

Runs on the HOST (not inside Docker). Checks:
  1. Recording file growth — alerts if output stalls
  2. Host memory — alerts if RAM usage too high (catches browser leaks)
  3. Disk space — alerts if free space too low

Designed to be started automatically by `make up` and stopped by `make down`.
Data collectors never interact with it directly.

Usage:
    python scripts/watchdog.py --slack-webhook URL
    python scripts/watchdog.py --slack-webhook URL --data-volume /data --interval 30
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import platform
import shutil
import subprocess
import sys
import time

import psutil
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s watchdog: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watchdog")

HOSTNAME = platform.node()


# ── Slack alerter with rate limiting ────────────────────────────


class SlackAlerter:
    """Send alerts to Slack with per-condition cooldown and recovery notifications."""

    def __init__(self, webhook_url: str | None, cooldown_s: int = 600):
        self._url = webhook_url
        self._cooldown_s = cooldown_s
        self._last_alert: dict[str, float] = {}
        self._active_alerts: set[str] = set()

    def alert(self, key: str, message: str):
        """Send an alert if cooldown has elapsed for this key."""
        now = time.monotonic()
        last = self._last_alert.get(key, 0)
        if now - last < self._cooldown_s:
            return  # Still in cooldown

        self._last_alert[key] = now
        self._active_alerts.add(key)
        log.warning("ALERT [%s]: %s", key, message)
        self._send(message)

    def recover(self, key: str, message: str):
        """Send a recovery notification only if this key was previously alerting."""
        if key not in self._active_alerts:
            return
        self._active_alerts.discard(key)
        self._last_alert.pop(key, None)
        log.info("RECOVERED [%s]: %s", key, message)
        self._send(message)

    def _send(self, text: str):
        if not self._url:
            return
        try:
            r = requests.post(self._url, json={"text": text}, timeout=10)
            if r.status_code != 200:
                log.error("Slack webhook returned %d: %s", r.status_code, r.text[:200])
        except Exception as exc:
            log.error("Slack webhook failed: %s", exc)


# ── Check functions ─────────────────────────────────────────────


def is_container_running(service: str = "mocap") -> bool:
    """Check if the Docker Compose service is running."""
    try:
        out = subprocess.check_output(
            ["docker", "compose", "ps", "-q", service],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
        ).strip()
        return bool(out)
    except Exception:
        return False


def find_active_recording_dir(data_volume: str) -> str | None:
    """Find the directory with the most recently modified file."""
    newest_dir, newest_time = None, 0
    try:
        for root, dirs, files in os.walk(data_volume):
            # Skip stress_test output
            if "stress_test" in root:
                continue
            for f in files:
                if not (f.endswith(".mp4") or f.endswith(".metadata.jsonl")):
                    continue
                path = os.path.join(root, f)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > newest_time:
                    newest_time = mtime
                    newest_dir = root
    except Exception:
        pass
    return newest_dir


def check_file_growth(data_volume: str, stale_threshold_s: int) -> tuple[bool, str]:
    """Check if recording output files are still being written.

    Returns (ok, detail). ok=True means files are growing or no recording is active.
    """
    recording_dir = find_active_recording_dir(data_volume)
    if not recording_dir:
        return True, "No recording output found"

    # Find newest file mtime
    newest_mtime = 0
    newest_file = ""
    for f in os.listdir(recording_dir):
        path = os.path.join(recording_dir, f)
        try:
            mtime = os.path.getmtime(path)
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest_file = f
        except OSError:
            continue

    if newest_mtime == 0:
        return True, "No files in recording directory yet"

    age_s = time.time() - newest_mtime
    last_modified = datetime.datetime.fromtimestamp(newest_mtime).strftime("%H:%M:%S")

    if age_s < stale_threshold_s:
        return True, f"Active — last write {age_s:.0f}s ago ({newest_file})"

    return False, (
        f":red_circle: *RECORDING STALLED*\n"
        f"Host: `{HOSTNAME}`\n"
        f"No file writes in {age_s:.0f}s (threshold: {stale_threshold_s}s)\n"
        f"Dir: `{recording_dir}`\n"
        f"Last modified: {last_modified} ({newest_file})"
    )


def check_host_memory(warn_pct: int) -> tuple[bool, str]:
    """Check total host RAM usage. Identifies top processes if over threshold.

    This is what would have caught Firefox at 8 GB before it reached 18.7 GB.
    PipelineMonitor._get_rss_mb() only reads /proc/self/status (container process).
    FrontendMonitor only monitors its own headless Chromium.
    Neither can see Firefox or any other host process.
    """
    mem = psutil.virtual_memory()
    pct = mem.percent
    used_gb = mem.used / (1024**3)
    total_gb = mem.total / (1024**3)

    if pct < warn_pct:
        return True, f"RAM {pct:.0f}% ({used_gb:.1f}/{total_gb:.0f} GB)"

    # Find top 3 memory consumers
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            rss = p.info["memory_info"].rss
            procs.append((p.info["name"], rss))
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue
    procs.sort(key=lambda x: x[1], reverse=True)
    top3 = procs[:3]
    top_lines = "\n".join(f"  {name:20s} {rss / (1024**3):.1f} GB" for name, rss in top3)

    return False, (
        f":large_yellow_circle: *HIGH MEMORY USAGE*\n"
        f"Host: `{HOSTNAME}`\n"
        f"RAM: {pct:.0f}% ({used_gb:.1f} / {total_gb:.0f} GB)\n"
        f"Top processes:\n```{top_lines}```"
    )


def check_disk_space(data_volume: str, warn_gb: int) -> tuple[bool, str]:
    """Check free disk space on the recording volume."""
    try:
        usage = shutil.disk_usage(data_volume)
    except OSError as exc:
        return False, f"Cannot read disk usage for {data_volume}: {exc}"

    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)

    if free_gb >= warn_gb:
        return True, f"Disk: {free_gb:.0f} GB free / {total_gb:.0f} GB total"

    return False, (
        f":large_yellow_circle: *LOW DISK SPACE*\n"
        f"Host: `{HOSTNAME}`\n"
        f"Free: {free_gb:.0f} GB / {total_gb:.0f} GB total\n"
        f"Volume: `{data_volume}`"
    )


# ── .env file loader ────────────────────────────────────────────


def load_dotenv(path: str) -> dict[str, str]:
    """Minimal .env parser — no dependencies."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


# ── Main loop ───────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Recording watchdog — monitors host health and alerts Slack")
    parser.add_argument("--slack-webhook", default=None, help="Slack incoming webhook URL (or set SLACK_WEBHOOK in .env)")
    parser.add_argument("--data-volume", default=None, help="Recording data root (default: from .env or /home/nbusleep/data)")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between checks (default: 30)")
    parser.add_argument("--stale-threshold", type=int, default=120, help="Seconds without file writes before alerting (default: 120)")
    parser.add_argument("--ram-warn-pct", type=int, default=80, help="Host RAM %% threshold (default: 80)")
    parser.add_argument("--disk-warn-gb", type=int, default=100, help="Free disk GB threshold (default: 100)")
    parser.add_argument("--cooldown", type=int, default=600, help="Seconds between repeat alerts for same condition (default: 600)")
    args = parser.parse_args()

    # Load .env for defaults
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    dotenv = load_dotenv(os.path.join(project_root, ".env"))

    webhook = args.slack_webhook or dotenv.get("SLACK_WEBHOOK")
    data_volume = args.data_volume or dotenv.get("DATA_VOLUME", "/home/nbusleep/data")

    if not webhook:
        log.warning("No Slack webhook configured — alerts will only be logged to stdout")
        log.warning("Set SLACK_WEBHOOK in .env or pass --slack-webhook URL")

    alerter = SlackAlerter(webhook, cooldown_s=args.cooldown)

    log.info("Watchdog started — interval=%ds, stale=%ds, ram_warn=%d%%, disk_warn=%dGB",
             args.interval, args.stale_threshold, args.ram_warn_pct, args.disk_warn_gb)
    log.info("Data volume: %s", data_volume)
    log.info("Slack webhook: %s", "configured" if webhook else "NOT configured")

    try:
        while True:
            container_up = is_container_running()

            # File growth — only when container is running
            if container_up:
                ok, detail = check_file_growth(data_volume, args.stale_threshold)
                if ok:
                    log.debug("file_growth: %s", detail)
                    alerter.recover("file_growth", f":white_check_mark: *Recording active*\nHost: `{HOSTNAME}`\nFile writes resumed")
                else:
                    alerter.alert("file_growth", detail)
            else:
                log.debug("Container not running — skipping file growth check")

            # Host memory — always
            ok, detail = check_host_memory(args.ram_warn_pct)
            if ok:
                log.debug("memory: %s", detail)
                alerter.recover("memory", f":white_check_mark: *Memory recovered*\nHost: `{HOSTNAME}`\n{detail}")
            else:
                alerter.alert("memory", detail)

            # Disk space — always
            ok, detail = check_disk_space(data_volume, args.disk_warn_gb)
            if ok:
                log.debug("disk: %s", detail)
                alerter.recover("disk", f":white_check_mark: *Disk space recovered*\nHost: `{HOSTNAME}`\n{detail}")
            else:
                alerter.alert("disk", detail)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        log.info("Watchdog stopped")


if __name__ == "__main__":
    main()
