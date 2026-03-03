"""Spool writer worker that persists per-frame raw data and enqueues encode jobs."""

from __future__ import annotations

import os
from queue import Queue

import cv2
import numpy as np
from tqdm import tqdm

from multi_camera.acquisition.flir.storage.encode_jobs_repo import EncodeJobsRepo


class SegmentSpoolWriter:
    def __init__(self, base_filename: str, serial: str):
        self.base_filename = base_filename
        self.serial = serial
        self.raw_path = f"{base_filename}.{serial}.frames.raw"
        self._fh = open(self.raw_path, "wb", buffering=1024 * 1024)
        self.width = None
        self.height = None
        self.pixel_format = None
        self.frame_count = 0

    def write_frame(self, frame: np.ndarray, pixel_format: str):
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)

        if self.width is None:
            self.height = int(frame.shape[0])
            self.width = int(frame.shape[1])
            self.pixel_format = pixel_format
        elif self.pixel_format != pixel_format:
            raise RuntimeError(f"pixel format changed within segment for {self.serial}: " f"{self.pixel_format} -> {pixel_format}")

        self._fh.write(memoryview(frame))
        self.frame_count += 1

    def close(self):
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()


def _flush_segment_to_encode_job(
    repo: EncodeJobsRepo,
    spool: SegmentSpoolWriter,
    acquisition_fps: float,
):
    if spool is None:
        return

    spool.close()
    if spool.frame_count <= 0:
        try:
            os.remove(spool.raw_path)
        except FileNotFoundError:
            pass
        return

    repo.enqueue_job(
        segment_base=spool.base_filename,
        camera_serial=spool.serial,
        raw_path=spool.raw_path,
        output_mp4=f"{spool.base_filename}.{spool.serial}.mp4",
        width=int(spool.width),
        height=int(spool.height),
        fps=float(acquisition_fps),
        pixel_format=spool.pixel_format,
    )


def write_spool_queue(
    image_queue: Queue,
    serial: str,
    pixel_format: str,
    acquisition_fps: float,
    encode_jobs_db: str,
):
    """
    Drain image queue into per-segment raw spool files and enqueue durable encode jobs.
    """
    current_base = None
    spool = None
    repo = EncodeJobsRepo(encode_jobs_db)

    try:
        while True:
            frame = image_queue.get()
            try:
                if frame is None:
                    break

                base_filename = frame["base_filename"]
                if base_filename != current_base:
                    _flush_segment_to_encode_job(repo, spool, acquisition_fps)
                    current_base = base_filename
                    spool = SegmentSpoolWriter(base_filename=current_base, serial=serial)

                im = frame["im"]
                if pixel_format == "BayerRG8":
                    im = cv2.cvtColor(im, cv2.COLOR_BAYER_RG2RGB)
                    spool_pixel_format = "rgb24"
                elif im.ndim == 2:
                    spool_pixel_format = "gray"
                elif im.ndim == 3 and im.shape[2] == 3:
                    # OpenCV arrays are conventionally BGR unless explicitly converted.
                    spool_pixel_format = "bgr24"
                else:
                    raise RuntimeError(f"Unsupported frame shape for spool writer: {im.shape}")

                spool.write_frame(im, spool_pixel_format)
            except Exception as exc:
                tqdm.write(f"write_spool_queue error ({serial}): {exc}")
            finally:
                image_queue.task_done()
    finally:
        _flush_segment_to_encode_job(repo, spool, acquisition_fps)
