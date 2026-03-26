"""Unified encoder worker: pipes raw Bayer frames to ffmpeg (NVENC or libx264) producing fragmented MP4."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
from queue import Queue

log = logging.getLogger("flir_pipeline")

# Map FLIR Bayer pattern names to ffmpeg pixel format names.
_BAYER_FFMPEG_PIXFMT = {
    "BayerRG8": "bayer_rggb8",
    "BayerBG8": "bayer_bggr8",
    "BayerGR8": "bayer_grbg8",
    "BayerGB8": "bayer_gbrg8",
}


def _build_ffmpeg_cmd(output_path: str, width: int, height: int, fps: float, pixel_format: str, use_nvenc: bool, preset: str) -> list[str]:
    """Build the ffmpeg command for encoding raw Bayer input to fragmented MP4."""
    pix_fmt = _BAYER_FFMPEG_PIXFMT.get(pixel_format, "gray")

    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pixel_format",
        pix_fmt,
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        "pipe:0",
    ]

    if use_nvenc:
        # VBR with constant quality (-cq) is NVENC's closest equivalent to libx264 -crf.
        # -cq 24 ≈ libx264 -crf 18 in file size. Visually indistinguishable for pose estimation.
        # constqp (-qp) doesn't adapt per-frame and produces much larger files.
        # -b:v 0 is required — without it ffmpeg defaults to 2 Mbps which acts as a floor.
        cmd += ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", "24", "-b:v", "0"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]

    # Keyframe interval = 1 second (for fragmented MP4 granularity).
    gop = max(1, int(round(fps)))
    cmd += ["-g", str(gop)]

    # Fragmented MP4: crash-safe, always playable up to last keyframe.
    cmd += ["-movflags", "+frag_keyframe+delay_moov"]

    cmd.append(output_path)
    return cmd


def _open_ffmpeg(output_path: str, width: int, height: int, fps: float, pixel_format: str, use_nvenc: bool, preset: str) -> subprocess.Popen:
    """Open an ffmpeg subprocess for encoding."""
    cmd = _build_ffmpeg_cmd(output_path, width, height, fps, pixel_format, use_nvenc, preset)
    log.debug("ffmpeg cmd: %s", " ".join(cmd))
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _close_ffmpeg(proc: subprocess.Popen | None) -> None:
    """Close ffmpeg stdin and wait for it to finish writing the final fragments."""
    if proc is None:
        return

    try:
        proc.stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        pass

    stderr_bytes = b""
    try:
        stderr_bytes = proc.stderr.read()
    except Exception:
        pass

    proc.wait()

    if proc.returncode != 0:
        stderr_text = stderr_bytes.decode(errors="replace").strip()
        log.error("ffmpeg exited %d: %s", proc.returncode, stderr_text)


def encoder_worker(
    image_queue: Queue,
    serial: str,
    pixel_format: str,
    acquisition_fps: float,
    width: int,
    height: int,
    use_nvenc: bool,
    preset: str,
    worker_error_state: dict,
    stop_event: threading.Event,
    flush_done_event: threading.Event,
):
    """Consume image_queue and pipe raw Bayer frames to ffmpeg.

    Same interface contract as the old ``write_journal_queue``:
    - Drains image_queue with get(timeout=1.0), checks stop_event on Empty
    - Sentinel ``None`` triggers clean exit
    - Sets ``worker_error_state`` on fatal error
    - Signals ``flush_done_event`` in finally block (always fires)
    """
    current_base = None
    proc = None
    encoder_name = f"h264_nvenc {preset}" if use_nvenc else "libx264 veryfast"
    log.info("encoder_worker(%s): started, encoder=%s, %dx%d@%.0ffps", serial, encoder_name, width, height, acquisition_fps)

    try:
        while True:
            try:
                frame = image_queue.get(timeout=1.0)
            except queue.Empty:
                if stop_event.is_set():
                    break
                continue

            try:
                if frame is None:
                    break

                base_filename = frame["base_filename"]

                # Segment boundary: close old ffmpeg, open new one.
                # This blocks briefly (~100-500ms) while ffmpeg flushes its encoder
                # buffers.  We do this synchronously because NVENC on consumer GPUs
                # (GTX 1650) limits concurrent encode sessions — closing in the
                # background would double the session count and risk hitting the cap.
                if base_filename != current_base:
                    _close_ffmpeg(proc)
                    proc = None
                    current_base = base_filename
                    output_path = f"{current_base}.{serial}.mp4"
                    log.info("encoder_worker(%s): new segment %s", serial, output_path)
                    proc = _open_ffmpeg(output_path, width, height, acquisition_fps, pixel_format, use_nvenc, preset)

                proc.stdin.write(frame["im"].tobytes())

            except (BrokenPipeError, ValueError):
                # ffmpeg died — log and set error.
                err_msg = f"encoder_worker({serial}): ffmpeg pipe broken"
                log.error(err_msg)
                worker_error_state["message"] = err_msg
                worker_error_state["event"].set()
                break
            except Exception as exc:
                err_msg = f"encoder_worker({serial}): {exc}"
                log.error(err_msg)
                worker_error_state["message"] = err_msg
                worker_error_state["event"].set()
                break
            finally:
                image_queue.task_done()

    finally:
        _close_ffmpeg(proc)
        log.info("encoder_worker(%s): stopped", serial)
        flush_done_event.set()
