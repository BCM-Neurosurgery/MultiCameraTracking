"""Encoder worker that drains durable encode jobs and produces MP4 outputs."""

from __future__ import annotations

import os
import subprocess
import threading
import time

from tqdm import tqdm

from multi_camera.acquisition.flir.storage.encode_jobs_repo import EncodeJobsRepo


def encode_jobs_worker(
    encode_jobs_db: str,
    stop_event: threading.Event,
    worker_id: str,
):
    """
    Claim jobs from SQLite and encode raw spool files into MP4 outputs.

    Encode failures are tracked in job status but do not stop acquisition.
    """
    repo = EncodeJobsRepo(encode_jobs_db)
    conn = repo.connect()
    keep_spool_files = str(os.environ.get("KEEP_SPOOL_FILES", "0")).lower() in ("1", "true", "yes", "on")

    try:
        # Recover jobs that were claimed before a crash/restart.
        repo.reset_in_progress_jobs(conn)
        while True:
            if stop_event.is_set() and repo.count_pending(conn) == 0:
                break

            job = repo.claim_next_job(conn, worker_id=worker_id)
            if job is None:
                time.sleep(0.3)
                continue

            try:
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
                    job.pixel_format,
                    "-video_size",
                    f"{job.width}x{job.height}",
                    "-framerate",
                    str(job.fps),
                    "-i",
                    job.raw_path,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    tmp_out,
                ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                os.replace(tmp_out, job.output_mp4)
                if not keep_spool_files and os.path.exists(job.raw_path):
                    os.remove(job.raw_path)
                repo.mark_done(conn, job.job_id)
            except Exception as exc:
                if isinstance(exc, subprocess.CalledProcessError):
                    stderr_text = (exc.stderr or b"").decode(errors="replace").strip()
                    err = stderr_text or str(exc)
                else:
                    err = str(exc)
                tqdm.write(f"encode job failed ({worker_id}, job_id={job.job_id}): {err}")
                repo.mark_failed(conn, job.job_id, err)
    finally:
        conn.close()
