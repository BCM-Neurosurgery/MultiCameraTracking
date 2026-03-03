"""Queue/worker orchestration for recording sessions and graceful shutdown."""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass, field

from multi_camera.acquisition.flir.pipeline.queues import build_recorder_queues
from multi_camera.acquisition.flir.storage.encode_jobs_repo import EncodeJobsRepo, get_encode_jobs_db_path
from multi_camera.acquisition.flir.storage.finalize_jobs_repo import FinalizeJobsRepo, get_finalize_jobs_db_path
from multi_camera.acquisition.flir.workers.encode_worker import encode_jobs_worker
from multi_camera.acquisition.flir.workers.metadata_workers import metadata_finalize_queue, write_metadata_queue
from multi_camera.acquisition.flir.workers.spool_writer_worker import write_spool_queue
from multi_camera.acquisition.flir.workers.video_writer_worker import write_image_queue


@dataclass
class WorkerHandles:
    image_threads: list[threading.Thread] = field(default_factory=list)
    encode_threads: list[threading.Thread] = field(default_factory=list)
    metadata_writer_thread: threading.Thread | None = None
    metadata_finalize_thread: threading.Thread | None = None
    writers_started: bool = False


class RecorderService:
    """Queue and worker orchestration for FlirRecorder."""

    def __init__(self, recorder):
        self.recorder = recorder
        # ENCODE_ASYNC=1 switches video path from in-thread MP4 writing to:
        # image_queue -> raw spool files -> durable SQLite encode jobs -> ffmpeg workers.
        self.encode_async = str(os.environ.get("ENCODE_ASYNC", "0")).lower() in ("1", "true", "yes", "on")

    def initialize_queues(self, max_frames: int):
        camera_serials = [camera.DeviceSerialNumber for camera in self.recorder.cams]
        queues = build_recorder_queues(camera_serials=camera_serials, frame_queue_size=max_frames)
        self.recorder.image_queue_dict = queues.image_queues
        self.recorder.json_queue = queues.metadata_queue
        self.recorder.records_queue = queues.records_queue

    def start_workers(self, config_metadata: dict) -> WorkerHandles:
        handles = WorkerHandles()
        if self.recorder.video_base_file is None:
            return handles

        finalize_base_dir = self.recorder.video_path if getattr(self.recorder, "video_path", "") else "."
        self.recorder.finalize_jobs_db = get_finalize_jobs_db_path(finalize_base_dir)
        repo = FinalizeJobsRepo(self.recorder.finalize_jobs_db)
        repo.init_db()
        self.recorder.finalize_stop_event.clear()

        if self.encode_async:
            encode_base_dir = self.recorder.video_path if getattr(self.recorder, "video_path", "") else "."
            self.recorder.encode_jobs_db = get_encode_jobs_db_path(encode_base_dir)
            encode_repo = EncodeJobsRepo(self.recorder.encode_jobs_db)
            encode_repo.init_db()
            self.recorder.encode_stop_event.clear()

            num_encode_workers = int(os.environ.get("ENCODE_WORKERS", "1"))
            for idx in range(max(1, num_encode_workers)):
                worker_id = f"encode_worker_{idx}"
                thread = threading.Thread(
                    name=worker_id,
                    target=encode_jobs_worker,
                    kwargs={
                        "encode_jobs_db": self.recorder.encode_jobs_db,
                        "stop_event": self.recorder.encode_stop_event,
                        "worker_id": worker_id,
                    },
                )
                thread.start()
                handles.encode_threads.append(thread)

        for camera in self.recorder.cams:
            serial = camera.DeviceSerialNumber
            if self.encode_async:
                target = write_spool_queue
                kwargs = {
                    "image_queue": self.recorder.image_queue_dict[serial],
                    "serial": serial,
                    "pixel_format": self.recorder.pixel_format,
                    "acquisition_fps": camera.AcquisitionFrameRate,
                    "encode_jobs_db": self.recorder.encode_jobs_db,
                }
            else:
                target = write_image_queue
                kwargs = {
                    "vid_file": self.recorder.video_base_file,
                    "image_queue": self.recorder.image_queue_dict[serial],
                    "serial": serial,
                    "pixel_format": self.recorder.pixel_format,
                    "acquisition_fps": camera.AcquisitionFrameRate,
                    "acquisition_type": self.recorder.camera_config["acquisition-type"],
                    "video_segment_len": self.recorder.camera_config["acquisition-settings"]["video_segment_len"],
                }

            thread = threading.Thread(
                name=f"write_image_{serial}",
                target=target,
                kwargs=kwargs,
            )
            thread.start()
            handles.image_threads.append(thread)

        handles.metadata_finalize_thread = threading.Thread(
            name="write_metadata_finalize",
            target=metadata_finalize_queue,
            kwargs={
                "finalize_jobs_db": self.recorder.finalize_jobs_db,
                "records_queue": self.recorder.records_queue,
                "stop_event": self.recorder.finalize_stop_event,
                "worker_error_state": self.recorder.writer_error,
            },
        )
        handles.metadata_finalize_thread.start()

        handles.metadata_writer_thread = threading.Thread(
            name="write_metadata",
            target=write_metadata_queue,
            kwargs={
                "json_file": self.recorder.video_base_file,
                "json_queue": self.recorder.json_queue,
                "finalize_jobs_db": self.recorder.finalize_jobs_db,
                "config_metadata": config_metadata,
                "worker_error_state": self.recorder.writer_error,
            },
        )
        handles.metadata_writer_thread.start()
        handles.writers_started = True
        return handles

    def stop_workers(self, handles: WorkerHandles):
        if self.recorder.video_base_file is None or not handles.writers_started:
            return

        for serial, image_queue in self.recorder.image_queue_dict.items():
            image_queue.put(None)
            image_queue.join()

        if not self.recorder.writer_error["event"].is_set():
            self.recorder.json_queue.put(None)
            self.recorder.json_queue.join()
        elif handles.metadata_writer_thread is not None and handles.metadata_writer_thread.is_alive():
            try:
                self.recorder.json_queue.put(None, timeout=1.0)
            except queue.Full:
                pass

        if handles.metadata_writer_thread is not None:
            handles.metadata_writer_thread.join(timeout=10)

        self.recorder.finalize_stop_event.set()
        if handles.metadata_finalize_thread is not None:
            handles.metadata_finalize_thread.join(timeout=20)

        if self.encode_async and handles.encode_threads:
            self.recorder.encode_stop_event.set()
            for thread in handles.encode_threads:
                thread.join(timeout=30)

    def collect_records(self) -> list[dict]:
        records = []
        while not self.recorder.records_queue.empty():
            records.append(self.recorder.records_queue.get())
            self.recorder.records_queue.task_done()
        self.recorder.records_queue.join()
        return records
