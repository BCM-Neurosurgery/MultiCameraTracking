"""Journal writer worker: JPEG-compresses raw Bayer frames into length-prefixed journal files."""

from __future__ import annotations

import logging
import os
import queue
import struct
import threading
from queue import Queue

import cv2
import numpy as np

from multi_camera.acquisition.flir.storage.encode_jobs_repo import EncodeJobsRepo

log = logging.getLogger("flir_pipeline")


class SegmentJournalWriter:
    """Append length-prefixed JPEG-encoded Bayer frames to a .journal file."""

    def __init__(self, base_filename: str, serial: str):
        self.base_filename = base_filename
        self.serial = serial
        self.journal_path = f"{base_filename}.{serial}.journal"
        self._fh = open(self.journal_path, "wb", buffering=1024 * 1024)
        self._closed = False
        self.width: int | None = None
        self.height: int | None = None
        self.bayer_pattern: str | None = None
        self.frame_count = 0

    def write_frame(self, bayer_frame: np.ndarray, bayer_pattern: str, jpeg_quality: int = 95):
        if self.width is None:
            self.height = int(bayer_frame.shape[0])
            self.width = int(bayer_frame.shape[1])
            self.bayer_pattern = bayer_pattern

        ok, jpeg_buf = cv2.imencode(".jpg", bayer_frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        if not ok:
            raise RuntimeError(f"JPEG encode failed for {self.serial} frame {self.frame_count}")

        data = jpeg_buf.tobytes()
        self._fh.write(struct.pack("<I", len(data)))
        self._fh.write(data)
        self.frame_count += 1

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()


def _flush_journal_to_encode_job(
    repo: EncodeJobsRepo,
    journal: SegmentJournalWriter | None,
    acquisition_fps: float,
):
    if journal is None:
        return

    journal.close()
    if journal.frame_count > 0:
        journal_size_mb = os.path.getsize(journal.journal_path) / (1024 * 1024)
        avg_jpeg_kb = (journal_size_mb * 1024) / journal.frame_count if journal.frame_count else 0
        log.info("segment closed: %s (%d frames, %.1f MB)", journal.journal_path, journal.frame_count, journal_size_mb)
        log.debug("avg jpeg size: %.1f KB/frame", avg_jpeg_kb)
    if journal.frame_count <= 0:
        try:
            os.remove(journal.journal_path)
        except FileNotFoundError:
            pass
        return

    repo.enqueue_job(
        segment_base=journal.base_filename,
        camera_serial=journal.serial,
        journal_path=journal.journal_path,
        output_mp4=f"{journal.base_filename}.{journal.serial}.mp4",
        width=int(journal.width),
        height=int(journal.height),
        fps=float(acquisition_fps),
        bayer_pattern=journal.bayer_pattern,
        frame_count=journal.frame_count,
    )


def write_journal_queue(
    image_queue: Queue,
    serial: str,
    pixel_format: str,
    acquisition_fps: float,
    encode_jobs_db: str,
    worker_error_state: dict,
    stop_event: threading.Event,
    flush_done_event: threading.Event,
):
    """
    Drain image queue, JPEG-encode raw Bayer frames into per-segment journal files,
    and enqueue durable encode jobs for background H.264 encoding.
    """
    current_base = None
    journal = None
    repo = EncodeJobsRepo(encode_jobs_db)

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
                if base_filename != current_base:
                    _flush_journal_to_encode_job(repo, journal, acquisition_fps)
                    journal = None
                    current_base = base_filename
                    journal = SegmentJournalWriter(base_filename=current_base, serial=serial)

                im = frame["im"]
                journal.write_frame(im, bayer_pattern=pixel_format)
            except Exception as exc:
                err_msg = f"write_journal_queue error ({serial}): {exc}"
                log.error(err_msg)
                worker_error_state["message"] = err_msg
                worker_error_state["event"].set()
                break
            finally:
                image_queue.task_done()
    finally:
        try:
            _flush_journal_to_encode_job(repo, journal, acquisition_fps)
        except Exception as exc:
            log.error("flush error during cleanup (%s): %s", serial, exc)
        flush_done_event.set()
