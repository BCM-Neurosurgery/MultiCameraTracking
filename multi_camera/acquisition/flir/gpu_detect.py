"""GPU detection and NVENC preset auto-recommendation.

Results are cached at module level — GPU capabilities don't change at runtime.
"""

from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger("flir_pipeline")

_nvenc_cache: bool | None = None
_gpu_info_cache: dict | None = None
_preset_cache: dict[tuple[int, int], str] = {}


def detect_nvenc() -> bool:
    """Return True if ffmpeg h264_nvenc is available and functional.

    Runs a real test encode rather than string-matching encoder lists,
    so it catches driver issues, missing GPUs, and container passthrough
    failures.
    """
    global _nvenc_cache
    if _nvenc_cache is not None:
        return _nvenc_cache
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
                "nullsrc=s=1920x1200:d=0.04",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=10,
        )
        _nvenc_cache = result.returncode == 0
        return _nvenc_cache
    except Exception:
        _nvenc_cache = False
        return False


def detect_gpu_info() -> dict:
    """Return GPU name, VRAM, and driver version from nvidia-smi.

    Returns empty dict if nvidia-smi is unavailable.
    """
    global _gpu_info_cache
    if _gpu_info_cache is not None:
        return _gpu_info_cache
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            _gpu_info_cache = {}
            return _gpu_info_cache
        parts = [p.strip() for p in result.stdout.strip().split(",")]
        if len(parts) < 3:
            _gpu_info_cache = {}
            return _gpu_info_cache
        _gpu_info_cache = {"name": parts[0], "vram_mb": int(float(parts[1])), "driver": parts[2]}
        return _gpu_info_cache
    except Exception:
        _gpu_info_cache = {}
        return _gpu_info_cache


def recommend_preset(num_cameras: int, target_fps: int = 30, min_headroom: float = 0.4) -> str:
    """Benchmark NVENC and return the best quality preset with sufficient headroom.

    Runs *num_cameras* concurrent ffmpeg sessions encoding 30 frames each.
    Tests presets from best quality to fastest (p5, p4, p3, p1).
    Returns the first preset where measured per-session fps exceeds
    *target_fps* * (1 + *min_headroom*).

    Falls back to ``"p1"`` if no preset meets the threshold.
    Takes ~2 seconds total on first call; cached for subsequent calls.
    """
    cache_key = (num_cameras, target_fps)
    if cache_key in _preset_cache:
        return _preset_cache[cache_key]
    threshold = target_fps * (1 + min_headroom)
    candidates = ["p5", "p4", "p3", "p1"]

    for preset in candidates:
        per_session_fps = _benchmark_preset(num_cameras, preset, num_frames=100)
        log.info("benchmark: %d sessions, preset %s → %.1f fps/session (need %.1f)", num_cameras, preset, per_session_fps, threshold)
        if per_session_fps >= threshold:
            _preset_cache[cache_key] = preset
            return preset

    _preset_cache[cache_key] = "p1"
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
