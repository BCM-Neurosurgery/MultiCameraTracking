"""Preflight system checks for deployment validation."""

from __future__ import annotations

import os
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
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.04", "-c:v", "h264_nvenc", "-f", "null", "-"],
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
        "nullsrc=s=64x64:d=0.5",
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
