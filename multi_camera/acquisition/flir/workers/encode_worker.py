"""Encoder worker: reads Bayer JPEG journals and produces H.264 MP4 outputs."""

from __future__ import annotations

import logging
import os
import struct
import subprocess
import threading
import time

import cv2
import numpy as np

from multi_camera.acquisition.flir.storage.encode_jobs_repo import EncodeJobsRepo

log = logging.getLogger("flir_pipeline")

# Map bayer_pattern strings to ffmpeg pixel format names.
# ffmpeg handles debayering internally, avoiding Python-side cv2.cvtColor
# and reducing pipe data from 6.6 MB (RGB) to 2.2 MB (Bayer) per frame.
_BAYER_FFMPEG_PIXFMT = {
    "BayerRG8": "bayer_rggb8",
    "BayerBG8": "bayer_bggr8",
    "BayerGR8": "bayer_grbg8",
    "BayerGB8": "bayer_gbrg8",
}


def _iter_journal_frames(journal_path: str):
    """Yield JPEG-encoded byte buffers from a length-prefixed journal file.

    Each record is: [uint32_le length][jpeg_bytes].
    Stops gracefully on EOF or truncated record (crash tolerance).
    """
    with open(journal_path, "rb") as fh:
        while True:
            header = fh.read(4)
            if len(header) < 4:
                break
            length = struct.unpack("<I", header)[0]
            data = fh.read(length)
            if len(data) < length:
                break  # truncated final frame after crash
            yield data


def _encode_journal_to_mp4(job, keep_journal: bool):
    """Decode journal JPEGs and pipe raw Bayer frames to ffmpeg for demosaic + H.264 encoding."""
    is_bayer = job.bayer_pattern in _BAYER_FFMPEG_PIXFMT
    pix_fmt = _BAYER_FFMPEG_PIXFMT.get(job.bayer_pattern, "rgb24")

    tmp_out = job.output_mp4 + ".tmp.mp4"
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
        f"{job.width}x{job.height}",
        "-framerate",
        str(job.fps),
        "-i",
        "pipe:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        tmp_out,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    decoded_count = 0
    try:
        for jpeg_data in _iter_journal_frames(job.journal_path):
            frame = cv2.imdecode(np.frombuffer(jpeg_data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if frame is None:
                raise RuntimeError(f"imdecode failed at frame {decoded_count}")
            if not is_bayer:
                # Non-Bayer single-channel: replicate to 3-channel for gray pix_fmt fallback
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
            proc.stdin.write(frame.tobytes())
            decoded_count += 1
    except (BrokenPipeError, ValueError):
        # ffmpeg exited early — ValueError ("flush of closed file") surfaces
        # when Python's buffered IO marks the pipe as closed after a prior EPIPE.
        pass
    except Exception:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        raise

    # Close stdin and wait for ffmpeg to finish.
    # Avoid proc.communicate() — on Python 3.12 it tries to flush stdin
    # even after close(), raising ValueError ("flush of closed file").
    try:
        proc.stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        pass
    stderr = proc.stderr.read()
    proc.wait()

    if proc.returncode != 0:
        stderr_text = (stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {stderr_text}")

    if decoded_count != job.frame_count:
        log.warning("frame count mismatch for job %s: expected %d, decoded %d", job.job_id, job.frame_count, decoded_count)

    os.replace(tmp_out, job.output_mp4)

    if not keep_journal and os.path.exists(job.journal_path):
        os.remove(job.journal_path)


def encode_jobs_worker(
    encode_jobs_db: str,
    stop_event: threading.Event,
    worker_id: str,
):
    """
    Claim jobs from SQLite and encode Bayer JPEG journals into H.264 MP4 outputs.

    Encode failures are tracked in job status but do not stop acquisition.
    """
    repo = EncodeJobsRepo(encode_jobs_db)
    conn = repo.connect()
    keep_journal = str(os.environ.get("KEEP_JOURNAL_FILES", "0")).lower() in ("1", "true", "yes", "on")

    try:
        # reset_in_progress_jobs is called once by the main thread before
        # workers are spawned (see recorder_service.start_workers).
        while True:
            if stop_event.is_set() and repo.count_pending(conn) == 0:
                break

            job = repo.claim_next_job(conn, worker_id=worker_id)
            if job is None:
                time.sleep(0.3)
                continue

            try:
                t0 = time.monotonic()
                _encode_journal_to_mp4(job, keep_journal)
                elapsed = time.monotonic() - t0
                fps = job.frame_count / elapsed if elapsed > 0 else 0
                log.info("encoded job %s: %s (%.1fs, %.0f fps)", job.job_id, job.output_mp4, elapsed, fps)
                repo.mark_done(conn, job.job_id)
                pending = repo.count_pending(conn)
                log.debug("encode backlog: %d pending jobs", pending)
            except Exception as exc:
                err = str(exc)
                log.error("encode job failed (%s, job_id=%s): %s", worker_id, job.job_id, err)
                repo.mark_failed(conn, job.job_id, err)
                # Clean up tmp file on failure
                tmp_out = job.output_mp4 + ".tmp.mp4"
                if os.path.exists(tmp_out):
                    try:
                        os.remove(tmp_out)
                    except OSError:
                        pass
    finally:
        conn.close()
