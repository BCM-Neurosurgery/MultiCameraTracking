"""Endurance test: real cameras + worst-case encoding for 24/7 confidence.

Usage:
    make endurance                                 # 4-hour default
    make endurance ENDURANCE_DURATION=86400         # 24-hour soak
    make endurance ENDURANCE_DURATION=691200        # 8-day soak

Combines real camera acquisition (GigE, PTP sync, firmware stability) with
noise-injected frames (worst-case encoding load) over extended durations.
"""

import argparse
import asyncio
import glob
import os
import signal
import sys
import threading
import time
from datetime import datetime

from multi_camera.acquisition.stress_test._verify import verify_mp4_files, verify_metadata_files
from multi_camera.acquisition.stress_test._report import Report, PASS, FAIL, WARN
from multi_camera.acquisition.stress_test.__main__ import load_config, run_preflight, run_capacity
from multi_camera.acquisition.endurance_test._runner import EnduranceRecorder, EnduranceReport, SegmentCleaner
from multi_camera.acquisition.endurance_test._monitor import EnduranceMonitor
from multi_camera.acquisition.flir.storage.finalize_jobs_repo import get_finalize_jobs_db_path


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------


def run_camera_init(r: Report, recorder: EnduranceRecorder, config_path: str):
    """Phase 2: Initialize and validate real cameras."""
    r.header("Phase 2: Camera Initialization")

    r.log("  Configuring cameras (PySpin + PTP sync)...")
    asyncio.run(recorder.configure_cameras(config_file=config_path))

    num_found = len(recorder.cams)
    serials = [cam.DeviceSerialNumber for cam in recorder.cams]
    r.check("Cameras Found", f"{num_found}: {', '.join(serials)}", good=num_found > 0, bad=num_found == 0)
    if num_found == 0:
        r.issue("No cameras found — check GigE connections and Spinnaker SDK")
        return False

    for cam in recorder.cams:
        r.row(f"  {cam.DeviceSerialNumber}", f"{cam.Width}x{cam.Height} {cam.PixelFormat} @ {cam.AcquisitionFrameRate}fps", "")

    return True


def run_endurance_soak(
    r: Report, cfg: dict, recorder: EnduranceRecorder, output_dir: str, duration_s: float, segment_seconds: int, monitor_interval: float
):
    """Phase 3: Run the endurance soak test with real cameras."""
    duration_h = duration_s / 3600
    fps = cfg["fps"]
    segment_frames = int(fps * segment_seconds)
    expected_segments = max(1, int(duration_s / segment_seconds))

    r.header("Phase 3: Endurance Soak")
    r.log(f"  {cfg['num_cameras']} cameras, {fps} FPS, {duration_h:.1f} hours")
    r.log(f"  Noise injection: {'ON' if recorder.inject_noise else 'OFF'}")
    r.log(f"  Segment: {segment_seconds}s ({segment_frames} frames), ~{expected_segments} segments")
    r.log()

    # Override segment length in config for shorter boundaries.
    recorder.camera_config["acquisition-settings"]["video_segment_len"] = segment_frames

    # Prepare output directory.
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    recording_path = os.path.join(output_dir, f"endurance_{now}")
    max_frames = int(fps * duration_s)

    # Start segment cleanup thread.
    cleaner = SegmentCleaner(output_dir=output_dir, num_cameras=cfg["num_cameras"], keep_n=5, interval_s=segment_seconds * 2)

    # Start extended monitor.
    db_path = get_finalize_jobs_db_path(output_dir)
    monitor = EnduranceMonitor(interval_s=monitor_interval, db_path=db_path)

    # Timer to stop recording after duration.
    stop_timer = threading.Timer(duration_s, recorder.stop_acquisition)
    stop_timer.daemon = True

    t0 = time.monotonic()
    try:
        monitor.start()
        cleaner.start()
        stop_timer.start()

        records = recorder.start_acquisition(recording_path=recording_path, max_frames=max_frames)
    finally:
        stop_timer.cancel()
        monitor.stop()
        cleaner.stop_event.set()
        cleaner.join(timeout=30)

    wall_time = time.monotonic() - t0

    # Extract results from the capture loop.
    result = recorder._endurance_result
    if result is None:
        r.issue("Endurance capture loop did not produce results")
        return None

    frames_produced, segments_completed, timeout_count, max_depth, boundary_depths = result

    # Read dropped frames from health tracker.
    dropped = recorder._health.dropped_frames if recorder._health else 0

    # Encoder detection: matches RecorderService._detect_encoder() logic.
    force_cpu = os.environ.get("FORCE_CPU_ENCODE", "").lower() in ("1", "true", "yes")
    config_preset = cfg["raw"].get("acquisition-settings", {}).get("nvenc_preset", "auto")
    if force_cpu or config_preset == "cpu":
        encoder_name = "libx264 (forced)"
    else:
        from multi_camera.acquisition.flir.gpu_detect import detect_nvenc

        encoder_name = "h264_nvenc" if detect_nvenc() else "libx264 (fallback)"

    report = EnduranceReport(
        num_cameras=cfg["num_cameras"],
        target_fps=fps,
        duration_s=duration_s,
        total_frames_produced=frames_produced,
        total_frames_expected=max_frames,
        dropped_frames=dropped,
        max_queue_depth=max_depth,
        actual_fps=frames_produced / wall_time if wall_time > 0 else 0,
        wall_time_s=wall_time,
        encoder=encoder_name,
        segments_completed=segments_completed,
        segment_frames=segment_frames,
        output_dir=output_dir,
        inject_noise=recorder.inject_noise,
        timeout_count=timeout_count,
        boundary_queue_depths=boundary_depths,
        monitor=monitor,
        segments_verified=cleaner.verified_count,
        segments_verify_failed=cleaner.failed_count,
    )

    # --- Report results ---

    r.row("Duration", f"{wall_time / 3600:.2f} hours ({wall_time:.0f}s)", "")
    r.row("Frames", f"{frames_produced:,} produced / {max_frames:,} expected", "")
    r.row("Segments", f"{segments_completed} completed", "")
    r.row("Encoder", encoder_name, "")

    # Dropped frames
    r.check("Drops", str(dropped), good=dropped == 0, bad=dropped > 0)
    if dropped > 0:
        drop_rate = dropped / max(1, frames_produced * cfg["num_cameras"]) * 100
        r.issue(f"{dropped} frames dropped ({drop_rate:.2f}%) during endurance soak")

    # Camera timeouts
    timeout_pct = timeout_count / max(1, frames_produced * cfg["num_cameras"]) * 100
    r.check("Camera Timeouts", f"{timeout_count} ({timeout_pct:.2f}%)", good=timeout_pct < 1, bad=timeout_pct > 5)
    if timeout_pct > 5:
        r.issue(f"Camera timeouts at {timeout_pct:.1f}% — check GigE connections")
    elif timeout_pct > 1:
        r.issue(f"Camera timeouts at {timeout_pct:.1f}% — occasional packet loss")

    # Queue utilization
    max_q = max(max_depth.values()) if max_depth else 0
    queue_size = cfg["queue_size"]
    pct = max_q / queue_size * 100
    r.check("Max Queue", f"{max_q}/{queue_size} ({pct:.0f}%)", good=pct < 50, bad=pct >= 90)
    if pct >= 90:
        r.issue(f"Queue nearly full ({pct:.0f}%)")

    # Segment boundary spike
    if boundary_depths:
        worst = max(max(snap.values()) for snap in boundary_depths)
        bpct = worst / queue_size * 100
        r.check("Boundary Spike", f"{worst}/{queue_size} ({bpct:.0f}%)", good=bpct < 50, bad=bpct >= 80)
        if bpct >= 80:
            r.issue(f"Segment boundaries spike to {bpct:.0f}% queue capacity")

    # GPU thermal
    mon = report.monitor
    if mon and mon.gpu_temp_samples:
        t_first = mon.gpu_temp_samples[0][1]
        r.check("GPU Temp", f"{t_first}→{mon.gpu_max_temp}→{mon.gpu_final_temp}C", good=not mon.gpu_throttled, bad=mon.gpu_throttled)
        if mon.gpu_throttled:
            r.issue(f"GPU reached {mon.gpu_max_temp}C — thermal throttling likely")

    # Memory trend
    if mon and len(mon.rss_samples) >= 2:
        growth = mon.rss_growth_rate_mb_per_min
        steady = [(t, rss) for t, rss in mon.rss_samples if t >= 60]
        rss_start = steady[0][1] if steady else mon.rss_samples[0][1]
        rss_end = steady[-1][1] if steady else mon.rss_samples[-1][1]
        if growth < 1:
            r.row("Memory Trend", f"{rss_start:.0f} -> {rss_end:.0f} MB (stable)", PASS)
        elif growth < 10:
            r.row("Memory Trend", f"+{growth:.1f} MB/min", WARN)
            r.issue(f"Memory growing at {growth:.1f} MB/min — possible leak")
        else:
            r.row("Memory Trend", f"+{growth:.1f} MB/min", FAIL)
            r.issue(f"Memory growing at {growth:.1f} MB/min — likely leak")

    # Thread count trend
    if mon and len(mon.thread_count_samples) >= 2:
        tc_growth = mon.thread_count_growth_per_hour
        tc_start = mon.thread_count_samples[0][1]
        tc_end = mon.thread_count_samples[-1][1]
        if tc_growth <= 0:
            r.row("Thread Count", f"{tc_start} -> {tc_end} (stable)", PASS)
        else:
            r.row("Thread Count", f"{tc_start} -> {tc_end} (+{tc_growth:.1f}/hr)", FAIL)
            r.issue(f"Thread count growing at {tc_growth:.1f}/hr — thread leak")

    # FD count trend
    if mon and len(mon.fd_count_samples) >= 2:
        fd_growth = mon.fd_count_growth_per_hour
        fd_start = mon.fd_count_samples[0][1]
        fd_end = mon.fd_count_samples[-1][1]
        if fd_growth <= 0:
            r.row("FD Count", f"{fd_start} -> {fd_end} (stable)", PASS)
        elif fd_growth <= 5:
            r.row("FD Count", f"{fd_start} -> {fd_end} (+{fd_growth:.1f}/hr)", WARN)
            r.issue(f"File descriptors growing at {fd_growth:.1f}/hr")
        else:
            r.row("FD Count", f"{fd_start} -> {fd_end} (+{fd_growth:.1f}/hr)", FAIL)
            r.issue(f"File descriptors growing at {fd_growth:.1f}/hr — descriptor leak")

    # SQLite DB size trend
    if mon and len(mon.db_size_samples) >= 2:
        db_start_kb = mon.db_size_samples[0][1] / 1024
        db_end_kb = mon.db_size_samples[-1][1] / 1024
        db_growth = mon.db_size_growth_kb_per_hour
        if db_growth <= 10:
            r.row("SQLite DB", f"{db_start_kb:.0f} -> {db_end_kb:.0f} KB (stable)", PASS)
        elif db_growth <= 100:
            r.row("SQLite DB", f"{db_start_kb:.0f} -> {db_end_kb:.0f} KB (+{db_growth:.0f} KB/hr)", WARN)
            r.issue(f"SQLite DB growing at {db_growth:.0f} KB/hr — check job cleanup")
        else:
            r.row("SQLite DB", f"{db_start_kb:.0f} -> {db_end_kb:.0f} KB (+{db_growth:.0f} KB/hr)", FAIL)
            r.issue(f"SQLite DB growing at {db_growth:.0f} KB/hr — completed jobs not being cleaned")

    # Segment cleanup stats
    total_seg_checked = cleaner.verified_count + cleaner.failed_count
    if total_seg_checked > 0:
        r.check(
            "Segment Cleanup",
            f"{cleaner.verified_count} verified, {cleaner.failed_count} failed",
            good=cleaner.failed_count == 0,
            bad=cleaner.failed_count > 0,
        )
        if cleaner.failed_count > 0:
            r.issue(f"{cleaner.failed_count} segments failed cleanup verification")

    r.json_data["checks"]["endurance_soak"] = {
        "frames_produced": frames_produced,
        "segments_completed": segments_completed,
        "timeout_count": timeout_count,
        "timeout_pct": timeout_pct,
        "max_queue_depth": max_q,
        "inject_noise": recorder.inject_noise,
        "encoder": encoder_name,
        "gpu_max_temp": mon.gpu_max_temp if mon else None,
        "gpu_throttled": mon.gpu_throttled if mon else None,
        "rss_growth_mb_per_min": mon.rss_growth_rate_mb_per_min if mon else None,
        "thread_count_growth_per_hour": mon.thread_count_growth_per_hour if mon else None,
        "fd_count_growth_per_hour": mon.fd_count_growth_per_hour if mon else None,
        "db_size_growth_kb_per_hour": mon.db_size_growth_kb_per_hour if mon and mon.db_size_samples else None,
        "segments_verified": cleaner.verified_count,
        "segments_verify_failed": cleaner.failed_count,
    }

    return report


def run_verification(r: Report, cfg: dict, output_dir: str, report: EnduranceReport):
    """Phase 4: Verify remaining output files."""
    r.header("Phase 4: Output Verification")

    # Only the last few segments remain (cleaner deleted the rest).
    r.log("  Verifying remaining MP4 files (ffprobe)...")
    mp4_issues, total_bytes = verify_mp4_files(output_dir, cfg["num_cameras"], report.segments_completed)
    mp4_count = len(glob.glob(os.path.join(output_dir, "*.mp4")))
    # Don't flag missing MP4s — cleaner intentionally deleted old ones.
    mp4_issues = [i for i in mp4_issues if "Expected at least" not in i]
    if not mp4_issues:
        r.row("MP4 Files", f"{mp4_count} remaining, all playable", PASS)
    else:
        for issue in mp4_issues:
            r.row("MP4 Files", issue, FAIL)
            r.issue(issue)

    r.log("  Verifying remaining metadata files...")
    meta_issues = verify_metadata_files(output_dir, report.segments_completed)
    meta_issues = [i for i in meta_issues if "Expected at least" not in i]
    jsonl_count = len(glob.glob(os.path.join(output_dir, "*.metadata.jsonl")))
    if not meta_issues:
        r.row("Metadata", f"{jsonl_count} remaining, valid", PASS)
    else:
        for issue in meta_issues:
            r.row("Metadata", issue, FAIL)
            r.issue(issue)

    return total_bytes


def run_verdict(r: Report, cfg: dict, report: EnduranceReport, duration_h: float):
    r.log()
    r.log("=" * 60)

    has_fatal = any(kw in i.lower() for i in r.issues for kw in ("failed", "invalid", "not detected", "throttl", "leak", "no cameras"))

    r.json_data["verdict"] = "FAIL" if has_fatal else ("WARN" if r.issues else "PASS")

    noise_str = "noise-injected " if report.inject_noise else ""
    if not r.issues:
        mon = report.monitor
        r.log(f"  {PASS} READY for {cfg['num_cameras']}-camera 24/7 deployment")
        r.log(f"  {PASS} {duration_h:.1f}h {noise_str}endurance: 0 drops, {report.segments_completed} segments")
        if mon and mon.gpu_temp_samples:
            r.log(f"  {PASS} GPU stable at {mon.gpu_final_temp}C")
        if mon and len(mon.rss_samples) >= 2:
            r.log(f"  {PASS} Memory stable, no leaks")
        if mon and len(mon.thread_count_samples) >= 2:
            r.log(f"  {PASS} Threads/FDs stable, no leaks")
    else:
        icon = FAIL if has_fatal else WARN
        label = "NOT READY" if has_fatal else "READY WITH WARNINGS"
        r.log(f"  {icon} {label} for {cfg['num_cameras']}-camera deployment")
        r.log()
        for issue in r.issues:
            r.log(f"    {WARN} {issue}")

    r.log("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Endurance test — real cameras + worst-case encoding for 24/7 confidence")
    parser.add_argument("--config", type=str, required=True, help="Path to camera config YAML")
    parser.add_argument("-d", "--duration", type=float, default=14400.0, help="Duration in seconds (default: 14400 = 4h)")
    parser.add_argument("--segment-seconds", type=int, default=120, help="Segment length in seconds (default: 120)")
    parser.add_argument("--no-inject-noise", action="store_true", help="Disable noise injection (default: noise ON)")
    parser.add_argument("--force-cpu", action="store_true", help="Force CPU encoding")
    parser.add_argument("--monitor-interval", type=float, default=60.0, help="Monitoring sample interval in seconds (default: 60)")
    args = parser.parse_args()
    args.data_volume = "/data"

    if args.force_cpu:
        os.environ["FORCE_CPU_ENCODE"] = "1"
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(1))

    inject_noise = not args.no_inject_noise
    cfg = load_config(args.config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = f"{timestamp}_{cfg['config_name']}"
    output_dir = os.path.join(args.data_volume, "endurance_test", run_name)
    os.makedirs(output_dir, exist_ok=True)

    duration_h = args.duration / 3600

    r = Report()
    r.json_data["config"] = cfg["config_name"]
    r.json_data["timestamp"] = timestamp
    r.json_data["test_type"] = "endurance"

    r.header("Endurance Test")
    r.log(f"  Config         {cfg['config_name']}.yaml")
    r.log(f"  Cameras        {cfg['num_cameras']}  |  {cfg['fps']} FPS  |  {cfg['mode']}")
    r.log(f"  Duration       {duration_h:.1f} hours")
    r.log(f"  Segment        {args.segment_seconds}s")
    r.log(f"  Noise inject   {'ON' if inject_noise else 'OFF'}")

    # Phase 1: Preflight (reused from stress_test)
    disk = run_preflight(r, cfg, args.data_volume)

    # Phase 2: Camera initialization
    recorder = EnduranceRecorder(inject_noise=inject_noise)
    report = None
    total_bytes = 0
    try:
        cameras_ok = run_camera_init(r, recorder, args.config)

        if cameras_ok:
            # Phase 3: Endurance soak
            report = run_endurance_soak(r, cfg, recorder, output_dir, args.duration, args.segment_seconds, args.monitor_interval)

            if report is not None:
                # Phase 4: Verification
                total_bytes = run_verification(r, cfg, output_dir, report)

                # Capacity estimate
                run_capacity(r, cfg, disk, total_bytes, report.wall_time_s)
        else:
            r.issue("Skipping soak — camera initialization failed")

        # Phase 5: Verdict
        if report is not None:
            run_verdict(r, cfg, report, duration_h)
        else:
            r.log()
            r.log("=" * 60)
            r.log(f"  {FAIL} NOT READY — endurance test could not run")
            r.log("=" * 60)
            r.json_data["verdict"] = "FAIL"
    finally:
        recorder.close()

    r.log()
    r.row("Report saved", output_dir, "")
    r.log()
    r.save(output_dir)

    # Clean up test recordings, keep reports.
    if "/endurance_test/" in output_dir:
        for f in os.listdir(output_dir):
            if not f.startswith("report."):
                try:
                    os.remove(os.path.join(output_dir, f))
                except OSError:
                    pass


if __name__ == "__main__":
    main()
