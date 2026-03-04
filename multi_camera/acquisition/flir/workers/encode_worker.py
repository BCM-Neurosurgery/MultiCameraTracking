"""Encoder worker: reads Bayer JPEG journals and produces H.264 MP4 outputs."""

from __future__ import annotations

import os
import struct
import subprocess
import threading
import time

import cv2
import numpy as np
from tqdm import tqdm

from multi_camera.acquisition.flir.storage.encode_jobs_repo import EncodeJobsRepo

# Map bayer_pattern strings to OpenCV demosaic codes
_BAYER_CVTCOLOR = {
    "BayerRG8": cv2.COLOR_BAYER_RG2RGB,
    "BayerBG8": cv2.COLOR_BAYER_BG2RGB,
    "BayerGR8": cv2.COLOR_BAYER_GR2RGB,
    "BayerGB8": cv2.COLOR_BAYER_GB2RGB,
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


def _encode_journal_to_mp4(job, keep_journal: bool, stop_event: threading.Event | None = None):
    """Decode journal JPEGs, debayer, and pipe RGB frames to ffmpeg for H.264 encoding."""
    cvt_code = _BAYER_CVTCOLOR.get(job.bayer_pattern)

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
        "rgb24",
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
            if stop_event is not None and stop_event.is_set():
                proc.kill()
                proc.wait()
                raise RuntimeError("encode interrupted by shutdown")
            bayer = cv2.imdecode(np.frombuffer(jpeg_data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if bayer is None:
                raise RuntimeError(f"imdecode failed at frame {decoded_count}")
            if cvt_code is not None:
                rgb = cv2.cvtColor(bayer, cvt_code)
            else:
                # Non-Bayer single-channel: replicate to 3-channel
                rgb = cv2.cvtColor(bayer, cv2.COLOR_GRAY2RGB)
            proc.stdin.write(rgb.tobytes())
            decoded_count += 1
        proc.stdin.close()
        _, stderr = proc.communicate()
    except BrokenPipeError:
        _, stderr = proc.communicate()
    except Exception:
        proc.kill()
        proc.wait()
        raise

    if proc.returncode != 0:
        stderr_text = (stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {stderr_text}")

    if decoded_count != job.frame_count:
        tqdm.write(f"frame count mismatch for job {job.job_id}: " f"expected {job.frame_count}, decoded {decoded_count}")

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
                _encode_journal_to_mp4(job, keep_journal, stop_event=stop_event)
                repo.mark_done(conn, job.job_id)
            except Exception as exc:
                err = str(exc)
                tqdm.write(f"encode job failed ({worker_id}, job_id={job.job_id}): {err}")
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
