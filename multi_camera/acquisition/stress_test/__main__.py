"""Deployment validation for the FLIR recording pipeline.

Loads the real camera config YAML, runs hardware checks, disk I/O benchmark,
a full pipeline stress test with worst-case frames, and verifies output integrity.

Usage:
    make validate                          # uses default camera_config.yaml
    make validate CONFIG=my_config.yaml    # use a specific config
    make validate DURATION=600             # 10-minute soak
"""

import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

import yaml

from multi_camera.acquisition.stress_test._runner import run_stress_test

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

W = 60
PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"


def _header(title: str):
    print()
    print("═" * W)
    print(f"  {title}")
    print("═" * W)


def _row(label: str, value: str, status: str = ""):
    print(f"  {label:<18s} {value:<28s} {status}")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_config(config_path: str) -> dict:
    """Load camera config YAML and extract validation parameters."""
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    acq = cfg.get("acquisition-settings", {})
    return {
        "raw": cfg,
        "num_cameras": len(cfg.get("camera-info", {})),
        "fps": acq.get("frame_rate", 30),
        "segment_frames": acq.get("video_segment_len", 1000),
        "exposure": acq.get("exposure_time", 15000),
        "gamma": acq.get("gamma", None),
        "nvenc_preset": acq.get("nvenc_preset", "auto"),
        "queue_size": acq.get("image_queue_size", 150),
        "mode": cfg.get("acquisition-type", "continuous"),
        "config_name": os.path.splitext(os.path.basename(config_path))[0],
    }


# ---------------------------------------------------------------------------
# System checks
# ---------------------------------------------------------------------------


def check_gpu() -> dict:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return {}
        parts = [p.strip() for p in r.stdout.strip().split(",")]
        if len(parts) < 4:
            return {}
        return {"name": parts[0], "vram_total_mb": int(float(parts[1])), "vram_free_mb": int(float(parts[2])), "driver": parts[3]}
    except Exception:
        return {}


def check_nvenc() -> bool:
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.04", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def check_ram() -> dict:
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    info[parts[0].rstrip(":")] = int(parts[1]) // 1024
        return {"total_mb": info.get("MemTotal", 0), "available_mb": info.get("MemAvailable", 0)}
    except Exception:
        return {"total_mb": 0, "available_mb": 0}


def check_disk(path: str) -> dict:
    try:
        usage = shutil.disk_usage(path)
        return {"total_gb": usage.total / 1e9, "free_gb": usage.free / 1e9}
    except Exception:
        return {"total_gb": 0, "free_gb": 0}


def benchmark_disk_write(path: str, size_mb: int = 256) -> float:
    os.makedirs(path, exist_ok=True)
    test_file = os.path.join(path, ".disk_benchmark")
    block = os.urandom(1024 * 1024)
    try:
        t0 = time.monotonic()
        with open(test_file, "wb") as f:
            for _ in range(size_mb):
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
        elapsed = time.monotonic() - t0
        return size_mb / elapsed if elapsed > 0 else 0
    except Exception:
        return 0
    finally:
        try:
            os.remove(test_file)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Output verification
# ---------------------------------------------------------------------------


def verify_mp4_files(output_dir: str, num_cameras: int, expected_segments: int) -> tuple[list[str], int]:
    issues = []
    total_bytes = 0
    mp4s = sorted(glob.glob(os.path.join(output_dir, "*.mp4")))

    if not mp4s:
        issues.append("No MP4 files produced")
        return issues, 0

    expected_mp4s = num_cameras * expected_segments
    if len(mp4s) < expected_mp4s:
        issues.append(f"Expected {expected_mp4s} MP4s ({num_cameras} cams × {expected_segments} seg), got {len(mp4s)}")

    corrupt = 0
    for mp4 in mp4s:
        total_bytes += os.path.getsize(mp4)
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration,nb_streams", "-of", "csv=p=0", mp4],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode != 0 or not r.stdout.strip():
                corrupt += 1
        except Exception:
            corrupt += 1

    if corrupt > 0:
        issues.append(f"{corrupt}/{len(mp4s)} MP4 files failed ffprobe validation")

    return issues, total_bytes


def verify_metadata_files(output_dir: str, expected_segments: int, segment_frames: int) -> list[str]:
    issues = []
    jsonl_files = sorted(glob.glob(os.path.join(output_dir, "*.metadata.jsonl")))

    if not jsonl_files:
        issues.append("No .metadata.jsonl files produced")
        return issues

    if len(jsonl_files) < expected_segments:
        issues.append(f"Expected {expected_segments} metadata journals, found {len(jsonl_files)}")

    for jsonl_path in jsonl_files:
        line_count = 0
        parse_errors = 0
        try:
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    line_count += 1
                    try:
                        record = json.loads(line)
                        if "camera_serials" not in record or "timestamps" not in record:
                            parse_errors += 1
                    except json.JSONDecodeError:
                        parse_errors += 1
        except Exception as e:
            issues.append(f"Could not read {os.path.basename(jsonl_path)}: {e}")
            continue

        if parse_errors > 0:
            issues.append(f"{os.path.basename(jsonl_path)}: {parse_errors}/{line_count} lines failed JSON parse")

    # Verify aggregate JSON files
    json_files = sorted(glob.glob(os.path.join(output_dir, "*.json")))
    for json_path in json_files:
        if json_path.endswith(".metadata.jsonl"):
            continue
        try:
            with open(json_path) as f:
                data = json.load(f)
            if "real_times" not in data or "timestamps" not in data:
                issues.append(f"{os.path.basename(json_path)}: missing expected fields")
        except json.JSONDecodeError as e:
            issues.append(f"{os.path.basename(json_path)}: invalid JSON — {e}")
        except Exception as e:
            issues.append(f"{os.path.basename(json_path)}: {e}")

    return issues


def save_report(output_dir: str, lines: list[str]):
    """Save the validation report as a plain text file."""
    report_path = os.path.join(output_dir, "report.txt")
    # Strip ANSI color codes for the text file
    import re

    ansi_escape = re.compile(r"\033\[[0-9;]*m")
    with open(report_path, "w") as f:
        f.write(f"Deployment Validation — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        for line in lines:
            f.write(ansi_escape.sub("", line) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Deployment validation — load camera config, test hardware + pipeline, verify outputs",
    )
    parser.add_argument("--config", type=str, required=True, help="Path to camera config YAML (e.g. /configs/camera_config.yaml)")
    parser.add_argument("-d", "--duration", type=float, default=300.0, help="Stress test duration in seconds (default: 300 = 5 min)")
    parser.add_argument("-o", "--output-base", type=str, default="/data/validation", help="Base output directory (default: /data/validation)")
    parser.add_argument("--data-volume", type=str, default="/data", help="Data volume to benchmark disk I/O against (default: /data)")
    parser.add_argument("--force-cpu", action="store_true", help="Force CPU encoding")
    args = parser.parse_args()

    if args.force_cpu:
        os.environ["FORCE_CPU_ENCODE"] = "1"

    signal.signal(signal.SIGINT, lambda s, f: sys.exit(1))

    # ── Load config ─────────────────────────────────────────────
    cfg = load_config(args.config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join(args.output_base, f"{timestamp}_{cfg['config_name']}")
    os.makedirs(output_dir, exist_ok=True)

    report_lines = []

    def log(line=""):
        print(line)
        report_lines.append(line)

    def header(title):
        log()
        log("═" * W)
        log(f"  {title}")
        log("═" * W)

    def row(label, value, status=""):
        line = f"  {label:<18s} {value:<28s} {status}"
        log(line)

    issues = []
    duration_min = args.duration / 60

    # ── Config summary ──────────────────────────────────────────
    header("Config")
    row("File", cfg["config_name"] + ".yaml")
    row("Cameras", str(cfg["num_cameras"]))
    row("Frame Rate", f"{cfg['fps']} FPS")
    row("Exposure", f"{cfg['exposure']} µs")
    row("Gamma", str(cfg["gamma"]) if cfg["gamma"] is not None else "disabled")
    row("Segment", f"{cfg['segment_frames']} frames")
    row("NVENC Preset", cfg["nvenc_preset"])
    row("Queue Size", str(cfg["queue_size"]))
    row("Test Duration", f"{duration_min:.0f} min")

    # ── 1. Hardware ─────────────────────────────────────────────
    header("1/5  Hardware")

    gpu = check_gpu()
    if gpu:
        row("GPU", gpu["name"], PASS)
        row("VRAM", f"{gpu['vram_total_mb']} MB total, {gpu['vram_free_mb']} MB free", PASS)
        row("Driver", gpu["driver"], PASS)
    else:
        row("GPU", "not detected", FAIL)
        issues.append("No GPU detected — will fall back to CPU encoding (slow)")

    has_nvenc = check_nvenc()
    if has_nvenc:
        row("NVENC", "h264_nvenc available", PASS)
    else:
        row("NVENC", "not available", FAIL if gpu else WARN)
        if gpu:
            issues.append("GPU present but NVENC failed — check driver/container GPU passthrough")
        else:
            issues.append("No NVENC — CPU encoding only")

    ram = check_ram()
    queue_ram_mb = cfg["num_cameras"] * cfg["queue_size"] * 2.3
    total_ram_needed_mb = queue_ram_mb + 2048
    if ram["available_mb"] > total_ram_needed_mb:
        row("RAM", f"{ram['total_mb']} MB total, {ram['available_mb']} MB avail", PASS)
    else:
        row("RAM", f"{ram['total_mb']} MB total, {ram['available_mb']} MB avail", WARN)
        issues.append(f"RAM ({ram['available_mb']} MB) tight for {cfg['num_cameras']} cameras (need ~{total_ram_needed_mb:.0f} MB)")

    cpu_count = os.cpu_count() or 1
    row("CPU", f"{cpu_count} cores", PASS if cpu_count >= 4 else WARN)

    # ── 2. Disk ─────────────────────────────────────────────────
    header("2/5  Disk I/O")

    disk = check_disk(args.data_volume)
    if disk["free_gb"] > 100:
        row("Free Space", f"{disk['free_gb']:.0f} GB ({args.data_volume})", PASS)
    elif disk["free_gb"] > 10:
        row("Free Space", f"{disk['free_gb']:.0f} GB ({args.data_volume})", WARN)
        issues.append(f"Low disk: {disk['free_gb']:.0f} GB on {args.data_volume}")
    else:
        row("Free Space", f"{disk['free_gb']:.1f} GB ({args.data_volume})", FAIL)
        issues.append(f"Very low disk: {disk['free_gb']:.1f} GB on {args.data_volume}")

    log("  Benchmarking write speed on data volume...")
    write_speed = benchmark_disk_write(args.data_volume, size_mb=256)
    min_write_speed = cfg["num_cameras"] * 15
    if write_speed > min_write_speed:
        row("Write Speed", f"{write_speed:.0f} MB/s", PASS)
    else:
        row("Write Speed", f"{write_speed:.0f} MB/s", WARN)
        issues.append(f"Disk write ({write_speed:.0f} MB/s) may be tight for {cfg['num_cameras']} cameras")

    # ── 3. Pipeline stress test ─────────────────────────────────
    header("3/5  Pipeline Stress Test")
    log(f"  {cfg['num_cameras']} cameras, {cfg['fps']} FPS, {duration_min:.0f} min, worst-case noise")
    log(f"  Segment rollover every {cfg['segment_frames']} frames")
    log()

    report = run_stress_test(
        num_cameras=cfg["num_cameras"],
        fps=cfg["fps"],
        duration_s=args.duration,
        output_dir=output_dir,
        queue_size=cfg["queue_size"],
        segment_frames=cfg["segment_frames"],
    )

    log()
    if report.dropped_frames == 0:
        row("Drops", "0", PASS)
    else:
        drop_rate = report.dropped_frames / max(1, report.total_frames_produced * cfg["num_cameras"]) * 100
        row("Drops", f"{report.dropped_frames} ({drop_rate:.2f}%)", FAIL)
        issues.append(f"{report.dropped_frames} frames dropped during {duration_min:.0f}-min stress test")

    max_depth = max(report.max_queue_depth.values()) if report.max_queue_depth else 0
    queue_pct = max_depth / cfg["queue_size"] * 100
    if queue_pct < 50:
        row("Max Queue", f"{max_depth}/{cfg['queue_size']} ({queue_pct:.0f}%)", PASS)
    elif queue_pct < 90:
        row("Max Queue", f"{max_depth}/{cfg['queue_size']} ({queue_pct:.0f}%)", WARN)
        issues.append(f"Queue reached {queue_pct:.0f}% — risk of drops under sustained load")
    else:
        row("Max Queue", f"{max_depth}/{cfg['queue_size']} ({queue_pct:.0f}%)", FAIL)
        issues.append(f"Queue nearly full ({queue_pct:.0f}%) — will drop under sustained load")

    row("Encoder", report.encoder, PASS)
    row("Segments", str(report.segments_completed), "")

    encoding_fps = report.total_frames_produced / report.wall_time_s if report.wall_time_s > 0 else 0
    if encoding_fps >= cfg["fps"] * 0.95:
        row("Throughput", f"{encoding_fps:.1f} FPS", PASS)
    else:
        row("Throughput", f"{encoding_fps:.1f} FPS (target: {cfg['fps']})", WARN)

    # ── 4. Output verification ──────────────────────────────────
    header("4/5  Output Verification")

    log("  Verifying MP4 files (ffprobe)...")
    mp4_issues, total_mp4_bytes = verify_mp4_files(output_dir, cfg["num_cameras"], report.segments_completed)
    mp4_count = len(glob.glob(os.path.join(output_dir, "*.mp4")))
    if not mp4_issues:
        row("MP4 Files", f"{mp4_count} files, all playable", PASS)
    else:
        for issue in mp4_issues:
            row("MP4 Files", issue, FAIL)
        issues.extend(mp4_issues)

    log("  Verifying metadata files...")
    meta_issues = verify_metadata_files(output_dir, report.segments_completed, cfg["segment_frames"])
    jsonl_count = len(glob.glob(os.path.join(output_dir, "*.metadata.jsonl")))
    json_count = len([f for f in glob.glob(os.path.join(output_dir, "*.json")) if not f.endswith(".metadata.jsonl")])
    if not meta_issues:
        row("Metadata", f"{jsonl_count} journals + {json_count} JSON, valid", PASS)
    else:
        for issue in meta_issues:
            row("Metadata", issue, FAIL)
        issues.extend(meta_issues)

    # ── 5. Capacity estimates ───────────────────────────────────
    header("5/5  Capacity Estimates")

    if total_mp4_bytes > 0 and report.wall_time_s > 0:
        bitrate_mbps = (total_mp4_bytes * 8) / report.wall_time_s / 1e6
        per_cam_mbps = bitrate_mbps / cfg["num_cameras"]
        row("Bitrate", f"{bitrate_mbps:.1f} Mbps ({per_cam_mbps:.1f}/cam)", "")

        if disk["free_gb"] > 1:
            bytes_per_sec = total_mp4_bytes / report.wall_time_s
            recording_hours = (disk["free_gb"] * 1e9) / bytes_per_sec / 3600
            recording_days = recording_hours / 24
            if recording_days >= 3:
                row("Disk Capacity", f"~{recording_days:.0f} days of recording", PASS)
            elif recording_days >= 1:
                row("Disk Capacity", f"~{recording_days:.1f} days of recording", WARN)
                issues.append(f"Only ~{recording_days:.1f} days of disk capacity")
            else:
                row("Disk Capacity", f"~{recording_hours:.1f} hours of recording", FAIL)
                issues.append(f"Only ~{recording_hours:.1f} hours of disk capacity")

    queue_gb = cfg["num_cameras"] * cfg["queue_size"] * 2.3 / 1024
    row("Queue Memory", f"~{queue_gb:.1f} GB for {cfg['num_cameras']} cameras", "")

    # ── Verdict ─────────────────────────────────────────────────
    log()
    log("═" * W)

    if not issues:
        log(f"  {PASS} READY — validated for {cfg['num_cameras']}-camera 24/7 deployment")
        log(f"  {PASS} {duration_min:.0f}-min soak: 0 drops, all outputs verified")
    else:
        has_fatal = report.dropped_frames > 0 or any("failed" in i.lower() for i in issues) or any("invalid" in i.lower() for i in issues)
        icon = FAIL if has_fatal else WARN
        verdict = "NOT READY" if has_fatal else "READY WITH WARNINGS"
        log(f"  {icon} {verdict} for {cfg['num_cameras']}-camera deployment")
        log()
        for issue in issues:
            log(f"  {WARN} {issue}")

    log("═" * W)
    log()

    row("Results saved", output_dir, "")
    log()

    # Save report text file
    save_report(output_dir, report_lines)


if __name__ == "__main__":
    main()
