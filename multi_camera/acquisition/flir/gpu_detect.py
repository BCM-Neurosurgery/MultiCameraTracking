"""GPU detection and NVENC preset auto-recommendation."""

from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger("flir_pipeline")


def detect_nvenc() -> bool:
    """Return True if ffmpeg h264_nvenc is available and functional.

    Runs a real test encode rather than string-matching encoder lists,
    so it catches driver issues, missing GPUs, and container passthrough
    failures.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "nullsrc=s=64x64:d=0.04",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def detect_gpu_info() -> dict:
    """Return GPU name, VRAM, and driver version from nvidia-smi.

    Returns empty dict if nvidia-smi is unavailable.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {}
        parts = [p.strip() for p in result.stdout.strip().split(",")]
        if len(parts) < 3:
            return {}
        return {"name": parts[0], "vram_mb": int(float(parts[1])), "driver": parts[2]}
    except Exception:
        return {}


def recommend_preset(num_cameras: int, target_fps: int = 30, min_headroom: float = 0.4) -> str:
    """Benchmark NVENC and return the best quality preset with sufficient headroom.

    Runs *num_cameras* concurrent ffmpeg sessions encoding 30 frames each.
    Tests presets from best quality to fastest (p5, p4, p3, p1).
    Returns the first preset where measured per-session fps exceeds
    *target_fps* * (1 + *min_headroom*).

    Falls back to ``"p1"`` if no preset meets the threshold.
    Takes ~2 seconds total.
    """
    threshold = target_fps * (1 + min_headroom)
    candidates = ["p5", "p4", "p3", "p1"]

    for preset in candidates:
        per_session_fps = _benchmark_preset(num_cameras, preset, num_frames=100)
        log.info("benchmark: %d sessions, preset %s → %.1f fps/session (need %.1f)", num_cameras, preset, per_session_fps, threshold)
        if per_session_fps >= threshold:
            return preset

    return "p1"


def _benchmark_preset(num_sessions: int, preset: str, num_frames: int = 30) -> float:
    """Run *num_sessions* concurrent NVENC encodes and return per-session fps."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pixel_format",
        "bayer_rggb8",
        "-video_size",
        "1920x1200",
        "-framerate",
        "300",
        "-i",
        "/dev/zero",
        "-frames:v",
        str(num_frames),
        "-c:v",
        "h264_nvenc",
        "-preset",
        preset,
        "-rc",
        "constqp",
        "-qp",
        "18",
        "-f",
        "null",
        "-",
    ]

    procs = []
    t0 = time.monotonic()
    try:
        for _ in range(num_sessions):
            procs.append(subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        failed = 0
        for proc in procs:
            if proc.wait() != 0:
                failed += 1
    except Exception:
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass
        return 0.0

    elapsed = time.monotonic() - t0
    if elapsed <= 0 or failed > 0:
        return 0.0

    return num_frames / elapsed
