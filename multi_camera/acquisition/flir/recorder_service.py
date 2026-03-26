"""Queue/worker orchestration for recording sessions and graceful shutdown."""

from __future__ import annotations

import logging
import os
import queue
import threading
from dataclasses import dataclass, field

log = logging.getLogger("flir_pipeline")

from multi_camera.acquisition.flir.gpu_detect import detect_nvenc, detect_gpu_info, recommend_preset
from multi_camera.acquisition.flir.pipeline.queues import build_recorder_queues
from multi_camera.acquisition.flir.storage.finalize_jobs_repo import FinalizeJobsRepo, get_finalize_jobs_db_path
from multi_camera.acquisition.flir.workers.encoder_worker import encoder_worker
from multi_camera.acquisition.flir.workers.metadata_workers import metadata_finalize_queue, write_metadata_queue

# Default image queue buffer: absorbs transient stalls (segment rollover, disk flush, GPU jitter).
# 150 frames = 5 seconds at 30 fps (~2.3 MB/frame × 150 = ~345 MB per camera).
# With 8 cameras: ~2.76 GB total.  Sized to absorb ~1.5-2.5s segment boundary
# stalls even with 8 cameras and disk contention.  Hovers near empty during
# normal operation.  Override via acquisition-settings.image_queue_size in config YAML.
_DEFAULT_IMAGE_QUEUE_SIZE = 150


@dataclass
class WorkerHandles:
    image_threads: list[threading.Thread] = field(default_factory=list)
    image_flush_events: list[threading.Event] = field(default_factory=list)
    metadata_writer_thread: threading.Thread | None = None
    metadata_flush_event: threading.Event | None = None
    metadata_finalize_thread: threading.Thread | None = None
    writers_started: bool = False
    use_nvenc: bool = False
    preset: str = ""


class RecorderService:
    """Queue and worker orchestration for FlirRecorder."""

    def __init__(self, recorder):
        self.recorder = recorder

    def initialize_queues(self, max_frames: int):
        camera_serials = [camera.DeviceSerialNumber for camera in self.recorder.cams]
        acq_settings = (self.recorder.camera_config or {}).get("acquisition-settings", {})
        queue_size = int(acq_settings.get("image_queue_size", _DEFAULT_IMAGE_QUEUE_SIZE))
        queues = build_recorder_queues(camera_serials=camera_serials, frame_queue_size=queue_size)
        self.recorder.image_queue_dict = queues.image_queues
        self.recorder.json_queue = queues.metadata_queue
        self.recorder.records_queue = queues.records_queue

    def _detect_encoder(self) -> tuple[bool, str]:
        """Detect GPU and choose encoder settings.

        Returns (use_nvenc, preset).  Respects config overrides:
        - ``nvenc_preset: "cpu"`` → force CPU fallback
        - ``nvenc_preset: "p1"``..``"p7"`` → use that preset
        - ``nvenc_preset: "auto"`` or absent → auto-detect and benchmark
        """
        # Check for env var override first.
        if os.environ.get("FORCE_CPU_ENCODE", "").lower() in ("1", "true", "yes"):
            log.info("FORCE_CPU_ENCODE set — using CPU encoder")
            return False, "veryfast"

        # Check config file override.
        config_preset = ""
        if self.recorder.camera_config:
            config_preset = self.recorder.camera_config.get("acquisition-settings", {}).get("nvenc_preset", "auto")

        if config_preset == "cpu":
            log.info("nvenc_preset=cpu in config — using CPU encoder")
            return False, "veryfast"

        # Check if NVENC is available.
        if not detect_nvenc():
            log.warning("NVENC not available — using CPU encoder (frames may drop with many cameras)")
            return False, "veryfast"

        gpu_info = detect_gpu_info()
        if gpu_info:
            log.info("GPU: %s, VRAM: %d MB, driver: %s", gpu_info.get("name", "?"), gpu_info.get("vram_mb", 0), gpu_info.get("driver", "?"))

        # Explicit preset from config.
        if config_preset and config_preset != "auto":
            log.info("nvenc_preset=%s from config", config_preset)
            return True, config_preset

        # Auto-detect: benchmark to find optimal preset.
        num_cameras = len(self.recorder.cams)
        preset = recommend_preset(num_cameras=num_cameras)
        log.info("auto-selected preset: %s for %d cameras", preset, num_cameras)
        return True, preset

    def start_workers(self, config_metadata: dict) -> WorkerHandles:
        handles = WorkerHandles()
        if self.recorder.video_base_file is None:
            return handles

        # --- Metadata finalize jobs (unchanged) ---
        finalize_base_dir = self.recorder.video_path if getattr(self.recorder, "video_path", "") else "."
        self.recorder.finalize_jobs_db = get_finalize_jobs_db_path(finalize_base_dir)
        repo = FinalizeJobsRepo(self.recorder.finalize_jobs_db)
        repo.init_db()
        self.recorder.finalize_stop_event = threading.Event()

        conn = repo.connect()
        repo.reset_in_progress_jobs(conn)
        conn.close()

        # --- Detect encoder ---
        use_nvenc, preset = self._detect_encoder()
        handles.use_nvenc = use_nvenc
        handles.preset = preset

        # A shared stop_event for encoder threads (checked on queue.Empty timeout).
        self.recorder.encoder_stop_event = threading.Event()

        # --- Spawn one encoder thread per camera ---
        for camera in self.recorder.cams:
            serial = camera.DeviceSerialNumber
            flush_event = threading.Event()
            thread = threading.Thread(
                name=f"encoder_{serial}",
                target=encoder_worker,
                kwargs={
                    "image_queue": self.recorder.image_queue_dict[serial],
                    "serial": serial,
                    "pixel_format": self.recorder.pixel_format,
                    "acquisition_fps": camera.AcquisitionFrameRate,
                    "width": int(camera.Width),
                    "height": int(camera.Height),
                    "use_nvenc": use_nvenc,
                    "preset": preset,
                    "worker_error_state": self.recorder.writer_error,
                    "stop_event": self.recorder.encoder_stop_event,
                    "flush_done_event": flush_event,
                },
            )
            thread.start()
            handles.image_threads.append(thread)
            handles.image_flush_events.append(flush_event)

        # --- Metadata workers (unchanged) ---
        handles.metadata_finalize_thread = threading.Thread(
            name="write_metadata_finalize",
            target=metadata_finalize_queue,
            kwargs={
                "finalize_jobs_db": self.recorder.finalize_jobs_db,
                "records_queue": self.recorder.records_queue,
                "stop_event": self.recorder.finalize_stop_event,
            },
        )
        handles.metadata_finalize_thread.start()

        handles.metadata_flush_event = threading.Event()
        handles.metadata_writer_thread = threading.Thread(
            name="write_metadata",
            target=write_metadata_queue,
            kwargs={
                "json_file": self.recorder.video_base_file,
                "json_queue": self.recorder.json_queue,
                "finalize_jobs_db": self.recorder.finalize_jobs_db,
                "config_metadata": config_metadata,
                "worker_error_state": self.recorder.writer_error,
                "stop_event": self.recorder.finalize_stop_event,
                "flush_done_event": handles.metadata_flush_event,
            },
        )
        handles.metadata_writer_thread.start()
        handles.writers_started = True
        return handles

    def stop_workers(self, handles: WorkerHandles):
        if self.recorder.video_base_file is None or not handles.writers_started:
            return

        # --- Phase 1: stop encoder threads and wait for ffmpeg to finish ---
        for serial, image_queue in self.recorder.image_queue_dict.items():
            image_queue.put(None)

        for event in handles.image_flush_events:
            event.wait(timeout=30)

        for thread in handles.image_threads:
            thread.join(timeout=10)
            if thread.is_alive():
                log.warning("%s did not exit within timeout", thread.name)

        # --- Phase 2: stop metadata writer and wait for final flush ---
        if not self.recorder.writer_error["event"].is_set():
            self.recorder.json_queue.put(None)
        elif handles.metadata_writer_thread is not None and handles.metadata_writer_thread.is_alive():
            try:
                self.recorder.json_queue.put(None, timeout=1.0)
            except queue.Full:
                pass

        if handles.metadata_flush_event is not None:
            handles.metadata_flush_event.wait(timeout=30)

        if handles.metadata_writer_thread is not None:
            handles.metadata_writer_thread.join(timeout=5)
            if handles.metadata_writer_thread.is_alive():
                log.warning("%s did not exit within timeout", handles.metadata_writer_thread.name)

        # --- Phase 3: tell finalize worker to drain and exit ---
        self.recorder.finalize_stop_event.set()
        if handles.metadata_finalize_thread is not None:
            handles.metadata_finalize_thread.join(timeout=20)
            if handles.metadata_finalize_thread.is_alive():
                log.warning("%s did not exit within timeout", handles.metadata_finalize_thread.name)

    def collect_records(self) -> list[dict]:
        records = []
        while not self.recorder.records_queue.empty():
            records.append(self.recorder.records_queue.get())
            self.recorder.records_queue.task_done()
        self.recorder.records_queue.join()
        return records
