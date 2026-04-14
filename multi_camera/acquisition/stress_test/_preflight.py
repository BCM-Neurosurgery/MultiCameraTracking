"""Preflight system checks for deployment validation."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time


def check_gpu() -> dict:
    """Return GPU info dict or empty if unavailable."""
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
    """Return True if h264_nvenc is functional (single-session test encode)."""
    try:
        r = subprocess.run(
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
        return r.returncode == 0
    except Exception:
        return False


def check_nvenc_concurrent(num_sessions: int) -> tuple[bool, int]:
    """Launch *num_sessions* concurrent NVENC encodes. Returns (all_ok, succeeded_count)."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "nullsrc=s=1920x1200:d=0.5",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]
    procs = []
    try:
        for _ in range(num_sessions):
            procs.append(subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        succeeded = sum(1 for p in procs if p.wait(timeout=15) == 0)
    except Exception:
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass
        succeeded = 0
    return succeeded >= num_sessions, succeeded


def check_ram() -> dict:
    """Return total and available RAM in MB."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    info[parts[0].rstrip(":")] = int(parts[1]) // 1024
        return {"total_mb": info.get("MemTotal", 0), "available_mb": info.get("MemAvailable", 0)}
    except Exception:
        return {"total_mb": 0, "available_mb": 0}


def check_fd_limits(num_cameras: int) -> dict:
    """Check file descriptor limits against pipeline requirements."""
    required = num_cameras * 10 + 200
    soft = hard = 0
    try:
        with open("/proc/self/limits") as f:
            for line in f:
                if "open files" in line.lower():
                    parts = line.split()
                    soft, hard = int(parts[3]), int(parts[4])
                    break
    except Exception:
        pass
    return {"soft": soft, "hard": hard, "required": required, "sufficient": soft >= required}


def check_disk(path: str) -> dict:
    """Return total and free disk space in GB."""
    try:
        usage = shutil.disk_usage(path)
        return {"total_gb": usage.total / 1e9, "free_gb": usage.free / 1e9}
    except Exception:
        return {"total_gb": 0, "free_gb": 0}


def detect_volume_type(path: str) -> str:
    """Detect filesystem type by reading /proc/mounts."""
    try:
        real_path = os.path.realpath(path)
        best_match, best_fstype = "", "unknown"
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    mount_point, fstype = parts[1], parts[2]
                    if real_path.startswith(mount_point) and len(mount_point) > len(best_match):
                        best_match, best_fstype = mount_point, fstype
        return best_fstype
    except Exception:
        return "unknown"


def benchmark_disk_write(path: str, size_mb: int = 256) -> float:
    """Sequential write benchmark. Returns MB/s."""
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


def check_host_info() -> dict:
    """Return kernel, CPU model, and physical/logical core counts.

    Physical core count comes from `/proc/cpuinfo` core-id uniqueness;
    logical count is `os.cpu_count()`. Works inside the container as long
    as `/proc` is mounted (it is, by default).
    """
    info = {
        "kernel": platform.release(),
        "cpu_model": "unknown",
        "cores_physical": 0,
        "cores_logical": os.cpu_count() or 0,
    }
    try:
        physical = set()
        with open("/proc/cpuinfo") as f:
            current_pkg = None
            for line in f:
                if line.startswith("model name") and info["cpu_model"] == "unknown":
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                elif line.startswith("physical id"):
                    current_pkg = line.split(":", 1)[1].strip()
                elif line.startswith("core id") and current_pkg is not None:
                    physical.add((current_pkg, line.split(":", 1)[1].strip()))
        if physical:
            info["cores_physical"] = len(physical)
    except Exception:
        pass
    # Fallback if /proc/cpuinfo had no physical/core-id (e.g. some VMs).
    if info["cores_physical"] == 0:
        info["cores_physical"] = info["cores_logical"]
    return info


def check_cpu_capacity(num_cameras: int, host: dict | None = None) -> dict:
    """Rough rule: need ≥ 2 logical cores per camera for libx264 `veryfast`.

    Returns {logical, physical, required, sufficient, warn_only}.
    - sufficient: ≥ 2× num_cameras logical cores.
    - warn_only: ≥ 1× num_cameras logical cores (likely to keep up at
      `ultrafast` or similar).

    Pass a pre-fetched *host* dict (from ``check_host_info()``) to avoid
    re-reading /proc/cpuinfo when the caller already has it.
    """
    if host is None:
        host = check_host_info()
    logical = host["cores_logical"]
    required = num_cameras * 2
    sufficient = logical >= required
    warn_only = logical >= num_cameras
    return {
        "model": host["cpu_model"],
        "physical": host["cores_physical"],
        "logical": logical,
        "required": required,
        "sufficient": sufficient,
        "warn_only": warn_only and not sufficient,
    }


def benchmark_libx264(num_cameras: int, target_fps: float, width: int = 1920, height: int = 1200, min_headroom: float = 0.4) -> dict:
    """Benchmark libx264 presets under *num_cameras* concurrent encodes.

    Candidate order: medium → fast → faster → veryfast → superfast → ultrafast.
    Returns the slowest preset that sustains ≥ *target_fps* × (1 + *min_headroom*)
    per session, or "ultrafast" if none meet the threshold.

    Returned dict: {preset, per_session_fps, target, headroom, sufficient}.
    """
    threshold = target_fps * (1 + min_headroom)
    presets = ["medium", "fast", "faster", "veryfast", "superfast", "ultrafast"]

    def _bench(preset: str, num_frames: int = 60) -> float:
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
            f"{width}x{height}",
            "-framerate",
            "300",
            "-i",
            "/dev/zero",
            "-frames:v",
            str(num_frames),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            "18",
            "-f",
            "null",
            "-",
        ]
        procs = []
        t0 = time.monotonic()
        try:
            for _ in range(num_cameras):
                procs.append(subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            failed = sum(1 for p in procs if p.wait(timeout=60) != 0)
        except Exception:
            for p in procs:
                try:
                    p.kill()
                except OSError:
                    pass
            return 0.0
        elapsed = time.monotonic() - t0
        if elapsed <= 0 or failed > 0:
            return 0.0
        return num_frames / elapsed

    best_preset = "ultrafast"
    best_fps = 0.0
    for preset in presets:
        fps = _bench(preset)
        if fps >= threshold:
            return {
                "preset": preset,
                "per_session_fps": fps,
                "target": target_fps,
                "headroom": (fps / target_fps) - 1.0 if target_fps else 0.0,
                "sufficient": True,
            }
        if fps > best_fps:
            best_preset, best_fps = preset, fps

    return {
        "preset": best_preset,
        "per_session_fps": best_fps,
        "target": target_fps,
        "headroom": (best_fps / target_fps) - 1.0 if target_fps else 0.0,
        "sufficient": False,
    }


def benchmark_disk_metadata(path: str, num_files: int = 500) -> float:
    """Create/write/sync small files. Returns p99 latency in ms."""
    os.makedirs(path, exist_ok=True)
    tmp_dir = os.path.join(path, ".metadata_bench")
    os.makedirs(tmp_dir, exist_ok=True)
    latencies = []
    try:
        for i in range(num_files):
            fpath = os.path.join(tmp_dir, f"test_{i:04d}.tmp")
            t0 = time.monotonic()
            with open(fpath, "wb") as f:
                f.write(b"x" * 4096)
                f.flush()
                os.fsync(f.fileno())
            latencies.append((time.monotonic() - t0) * 1000)
        latencies.sort()
        return latencies[min(int(len(latencies) * 0.99), len(latencies) - 1)]
    except Exception:
        return 0.0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
