"""Deployment validation for the FLIR recording pipeline.

Usage:
    make validate                          # camera_config.yaml, 5-min soak
    make validate CONFIG=my_config.yaml    # specific config
    make validate DURATION=600             # 10-minute soak
"""

import argparse
import glob
import os
import shutil
import signal
import sys
from datetime import datetime

import yaml

from multi_camera.acquisition.stress_test._preflight import (
    check_gpu,
    check_nvenc,
    check_nvenc_concurrent,
    check_ram,
    check_fd_limits,
    check_disk,
    detect_volume_type,
    benchmark_disk_write,
    benchmark_disk_metadata,
)
from multi_camera.acquisition.stress_test._runner import run_stress_test
from multi_camera.acquisition.stress_test._verify import verify_mp4_files, verify_metadata_files
from multi_camera.acquisition.stress_test._report import Report, PASS, FAIL, WARN


def load_config(config_path: str) -> dict:
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
# Phase runners
# ---------------------------------------------------------------------------


def run_preflight(r: Report, cfg: dict, data_volume: str):
    r.header("Phase 1: Preflight")
    n = cfg["num_cameras"]

    # GPU + NVENC
    gpu = check_gpu()
    if gpu:
        r.row("GPU", gpu["name"], PASS)
        r.row("VRAM", f"{gpu['vram_total_mb']} MB total, {gpu['vram_free_mb']} MB free", PASS)
        r.row("Driver", gpu["driver"], PASS)
    else:
        r.row("GPU", "not detected", FAIL)
        r.issue("No GPU detected — CPU encoding only (slow, may not sustain load)")

    has_nvenc = check_nvenc()
    if has_nvenc:
        r.row("NVENC", "h264_nvenc available", PASS)
    else:
        r.row("NVENC", "not available", FAIL if gpu else WARN)
        if gpu:
            r.issue("GPU present but NVENC failed — check driver/container GPU passthrough")

    if has_nvenc and n > 1:
        r.log(f"  Testing {n} concurrent NVENC sessions...")
        all_ok, count = check_nvenc_concurrent(n)
        r.check("NVENC Sessions", f"{count}/{n} OK", good=all_ok, bad=not all_ok)
        if not all_ok:
            r.issue(f"GPU supports only {count} concurrent NVENC sessions, need {n}")

    # RAM
    ram = check_ram()
    needed_mb = n * cfg["queue_size"] * 2.3 + 2048
    r.check("RAM", f"{ram['total_mb']} MB total, {ram['available_mb']} MB avail", good=ram["available_mb"] > needed_mb)
    if ram["available_mb"] <= needed_mb:
        r.issue(f"RAM ({ram['available_mb']} MB) tight for {n} cameras (need ~{needed_mb:.0f} MB)")

    # CPU + FD limits
    cpu_count = os.cpu_count() or 1
    r.check("CPU", f"{cpu_count} cores", good=cpu_count >= 4)

    fd = check_fd_limits(n)
    r.check("FD Limit", f"{fd['soft']} (need {fd['required']})", good=fd["sufficient"])
    if not fd["sufficient"]:
        r.issue(f"File descriptor limit ({fd['soft']}) low for {n} cameras (need {fd['required']})")

    # Disk: volume type
    fs_type = detect_volume_type(data_volume)
    is_network_fs = fs_type in ("nfs", "nfs4", "cifs", "smb")
    r.check("Volume Type", f"{fs_type}{' (network)' if is_network_fs else ''}", good=not is_network_fs)
    if is_network_fs:
        r.issue(f"Data volume is {fs_type} — SQLite WAL may fail; latency spikes expected")

    # Disk: space
    disk = check_disk(data_volume)
    r.check("Free Space", f"{disk['free_gb']:.0f} GB", good=disk["free_gb"] > 100, bad=disk["free_gb"] <= 10)
    if disk["free_gb"] <= 100:
        r.issue(f"Low disk: {disk['free_gb']:.0f} GB on {data_volume}")

    # Disk: sequential write
    r.log("  Benchmarking sequential write (256 MB)...")
    write_mbps = benchmark_disk_write(data_volume, size_mb=256)
    min_write = n * 15
    r.check("Write Speed", f"{write_mbps:.0f} MB/s", good=write_mbps > min_write)
    if write_mbps <= min_write:
        r.issue(f"Disk write ({write_mbps:.0f} MB/s) tight for {n} cameras (need {min_write})")

    # Disk: metadata ops
    r.log("  Benchmarking metadata ops (500 file creates)...")
    p99 = benchmark_disk_metadata(data_volume, num_files=500)
    r.check("Metadata Latency", f"p99 = {p99:.1f} ms", good=p99 < 10, bad=p99 >= 50)
    if p99 >= 50:
        r.issue(f"Disk metadata latency high ({p99:.0f} ms p99) — segment rollovers may stall")

    r.json_data["checks"]["preflight"] = {
        "gpu": gpu,
        "nvenc": has_nvenc,
        "ram_available_mb": ram["available_mb"],
        "fd_soft": fd["soft"],
        "fs_type": fs_type,
        "disk_free_gb": disk["free_gb"],
        "write_speed_mbps": write_mbps,
        "metadata_p99_ms": p99,
    }
    return disk


def run_soak(r: Report, cfg: dict, output_dir: str, test_segment_frames: int, duration_s: float):
    duration_min = duration_s / 60
    r.header("Phase 2: Soak Test")
    r.log(f"  {cfg['num_cameras']} cameras, {cfg['fps']} FPS, {duration_min:.0f} min")
    r.log(f"  Worst-case noise frames, segment rollover every {test_segment_frames} frames")
    r.log()

    report = run_stress_test(
        num_cameras=cfg["num_cameras"],
        fps=cfg["fps"],
        duration_s=duration_s,
        output_dir=output_dir,
        queue_size=cfg["queue_size"],
        segment_frames=test_segment_frames,
    )
    r.log()

    # Drops
    r.check("Drops", str(report.dropped_frames), good=report.dropped_frames == 0, bad=report.dropped_frames > 0)
    if report.dropped_frames > 0:
        rate = report.dropped_frames / max(1, report.total_frames_produced * cfg["num_cameras"]) * 100
        r.issue(f"{report.dropped_frames} frames dropped ({rate:.2f}%) during {duration_min:.0f}-min soak")

    # Queue utilization
    max_depth = max(report.max_queue_depth.values()) if report.max_queue_depth else 0
    pct = max_depth / cfg["queue_size"] * 100
    r.check("Max Queue", f"{max_depth}/{cfg['queue_size']} ({pct:.0f}%)", good=pct < 50, bad=pct >= 90)
    if pct >= 90:
        r.issue(f"Queue nearly full ({pct:.0f}%)")

    r.row("Encoder", report.encoder, PASS)
    r.row("Segments", str(report.segments_completed), "")

    # Segment boundary spike
    if report.boundary_queue_depths:
        worst = max(max(snap.values()) for snap in report.boundary_queue_depths)
        bpct = worst / cfg["queue_size"] * 100
        r.check("Boundary Spike", f"{worst}/{cfg['queue_size']} ({bpct:.0f}%)", good=bpct < 50, bad=bpct >= 80)
        if bpct >= 80:
            r.issue(f"Segment boundaries spike to {bpct:.0f}% queue capacity")

    # GPU thermal
    mon = report.monitor
    if mon and mon.gpu_temp_samples:
        t0 = mon.gpu_temp_samples[0][1]
        r.check("GPU Temp", f"{t0}→{mon.gpu_max_temp}→{mon.gpu_final_temp}°C", good=not mon.gpu_throttled, bad=mon.gpu_throttled)
        if mon.gpu_throttled:
            r.issue(f"GPU reached {mon.gpu_max_temp}°C — thermal throttling likely")

    # Memory trend
    if mon and len(mon.rss_samples) >= 2:
        growth = mon.rss_growth_rate_mb_per_min
        # Show steady-state RSS, not the warmup spike
        steady = [(t, rss) for t, rss in mon.rss_samples if t >= 60]
        rss_start = steady[0][1] if steady else mon.rss_samples[0][1]
        rss_end = steady[-1][1] if steady else mon.rss_samples[-1][1]
        if growth < 1:
            r.row("Memory Trend", f"{rss_start:.0f}→{rss_end:.0f} MB (stable)", PASS)
        elif growth < 10:
            r.row("Memory Trend", f"+{growth:.1f} MB/min", WARN)
            r.issue(f"Memory growing at {growth:.1f} MB/min — possible leak")
        else:
            r.row("Memory Trend", f"+{growth:.1f} MB/min", FAIL)
            r.issue(f"Memory growing at {growth:.1f} MB/min — likely leak, will OOM in 24/7")

    r.json_data["checks"]["soak"] = {
        "drops": report.dropped_frames,
        "max_queue_depth": max_depth,
        "segments": report.segments_completed,
        "encoder": report.encoder,
        "gpu_max_temp": mon.gpu_max_temp if mon else None,
        "gpu_throttled": mon.gpu_throttled if mon else None,
        "rss_growth_mb_per_min": mon.rss_growth_rate_mb_per_min if mon else None,
    }
    return report


def run_verification(r: Report, cfg: dict, output_dir: str, report, test_segment_frames: int):
    r.header("Phase 3: Output Verification")

    r.log("  Verifying MP4 files (ffprobe)...")
    mp4_issues, total_bytes = verify_mp4_files(output_dir, cfg["num_cameras"], report.segments_completed)
    mp4_count = len(glob.glob(os.path.join(output_dir, "*.mp4")))
    if not mp4_issues:
        r.row("MP4 Files", f"{mp4_count} files, all playable", PASS)
    else:
        for issue in mp4_issues:
            r.row("MP4 Files", issue, FAIL)
            r.issue(issue)

    r.log("  Verifying metadata files...")
    meta_issues = verify_metadata_files(output_dir, report.segments_completed)
    jsonl_count = len(glob.glob(os.path.join(output_dir, "*.metadata.jsonl")))
    json_count = len([f for f in glob.glob(os.path.join(output_dir, "*.json")) if not f.endswith(".metadata.jsonl")])
    if not meta_issues:
        r.row("Metadata", f"{jsonl_count} journals + {json_count} JSON, valid", PASS)
    else:
        for issue in meta_issues:
            r.row("Metadata", issue, FAIL)
            r.issue(issue)

    return total_bytes


def run_capacity(r: Report, cfg: dict, disk: dict, total_mp4_bytes: int, wall_time_s: float):
    r.header("Capacity Estimates")

    if total_mp4_bytes > 0 and wall_time_s > 0:
        bitrate = (total_mp4_bytes * 8) / wall_time_s / 1e6
        r.row("Bitrate", f"{bitrate:.1f} Mbps ({bitrate / cfg['num_cameras']:.1f}/cam)", "")

        if disk["free_gb"] > 1:
            days = (disk["free_gb"] * 1e9) / (total_mp4_bytes / wall_time_s) / 86400
            r.check("Disk Capacity", f"~{days:.0f} days" if days >= 1 else f"~{days * 24:.1f} hours", good=days >= 3, bad=days < 1)
            if days < 3:
                r.issue(f"Only ~{days:.1f} days of disk capacity")

    queue_gb = cfg["num_cameras"] * cfg["queue_size"] * 2.3 / 1024
    r.row("Queue Memory", f"~{queue_gb:.1f} GB for {cfg['num_cameras']} cameras", "")


def run_verdict(r: Report, cfg: dict, report, duration_min: float):
    r.log()
    r.log("═" * 60)

    has_fatal = report.dropped_frames > 0 or any(kw in i.lower() for i in r.issues for kw in ("failed", "invalid", "not detected", "throttl", "leak"))

    r.json_data["verdict"] = "FAIL" if has_fatal else ("WARN" if r.issues else "PASS")

    if not r.issues:
        mon = report.monitor
        r.log(f"  {PASS} READY for {cfg['num_cameras']}-camera 24/7 deployment")
        r.log(f"  {PASS} {duration_min:.0f}-min soak: 0 drops, {report.segments_completed} segments, all outputs valid")
        if mon and mon.gpu_temp_samples:
            r.log(f"  {PASS} GPU stable at {mon.gpu_final_temp}°C")
        if mon and len(mon.rss_samples) >= 2:
            r.log(f"  {PASS} Memory stable, no leaks")
    else:
        icon = FAIL if has_fatal else WARN
        r.log(f"  {icon} {'NOT READY' if has_fatal else 'READY WITH WARNINGS'} for {cfg['num_cameras']}-camera deployment")
        r.log()
        for issue in r.issues:
            r.log(f"    {WARN} {issue}")

    r.log("═" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Deployment validation — hardware, disk, pipeline soak, output verification")
    parser.add_argument("--config", type=str, required=True, help="Path to camera config YAML")
    parser.add_argument("-d", "--duration", type=float, default=300.0, help="Soak duration in seconds (default: 300 = 5 min)")
    parser.add_argument("--force-cpu", action="store_true", help="Force CPU encoding")
    args = parser.parse_args()
    args.data_volume = "/data"  # Always /data inside the container (mounted from $DATA_VOLUME)

    if args.force_cpu:
        os.environ["FORCE_CPU_ENCODE"] = "1"
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(1))

    cfg = load_config(args.config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = f"{timestamp}_{cfg['config_name']}"

    # Everything goes to one directory on the data volume.
    # Test recordings are deleted after verification; reports are kept.
    output_dir = os.path.join(args.data_volume, "stress_test", run_name)
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir, ignore_errors=True)
    os.makedirs(output_dir, exist_ok=True)

    # Cap test segment size to ensure boundaries are exercised within the soak window
    test_seg = min(cfg["segment_frames"], 1000) if cfg["segment_frames"] > 0 else 1000
    duration_min = args.duration / 60

    r = Report()
    r.json_data["config"] = cfg["config_name"]
    r.json_data["timestamp"] = timestamp

    r.header("Deployment Validation")
    r.log(f"  Config         {cfg['config_name']}.yaml")
    r.log(f"  Cameras        {cfg['num_cameras']}  |  {cfg['fps']} FPS  |  {cfg['mode']}")
    r.log(f"  Segment        {cfg['segment_frames']} frames (test: {test_seg})")
    r.log(f"  Soak           {duration_min:.0f} min")

    disk = run_preflight(r, cfg, args.data_volume)
    report = run_soak(r, cfg, output_dir, test_seg, args.duration)
    total_bytes = run_verification(r, cfg, output_dir, report, test_seg)
    run_capacity(r, cfg, disk, total_bytes, report.wall_time_s)
    run_verdict(r, cfg, report, duration_min)

    # Save reports before cleanup
    r.log()
    r.row("Report saved", output_dir, "")
    r.log()
    r.save(output_dir)

    # Delete test recordings (MP4s, metadata), keep only report.txt + report.json.
    # Safety: only delete if path is under a stress_test/ directory we created.
    if "/stress_test/" in output_dir:
        for f in os.listdir(output_dir):
            if not f.startswith("report."):
                os.remove(os.path.join(output_dir, f))


if __name__ == "__main__":
    main()
