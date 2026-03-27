"""Stress test runner — exercises the full recording pipeline with synthetic frames.

Includes background monitoring of GPU temperature and process memory to detect
thermal throttling and memory leaks during sustained operation.
"""

from __future__ import annotations

import logging
import os
import resource
import subprocess
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from tqdm import tqdm

from multi_camera.acquisition.flir.pipeline.queues import safe_put, put_metadata_or_fail
from multi_camera.acquisition.flir.recorder_service import RecorderService
from multi_camera.acquisition.flir.logging_setup import setup_recording_logger
from multi_camera.acquisition.flir.pipeline.health import PipelineHealth

log = logging.getLogger("flir_pipeline")


# ---------------------------------------------------------------------------
# Shim objects that duck-type what RecorderService and workers expect
# ---------------------------------------------------------------------------


@dataclass
class FakeCamera:
    DeviceSerialNumber: str
    AcquisitionFrameRate: float
    Width: int
    Height: int
    ExposureTime: float = 15000.0
    BinningHorizontal: int = 1
    BinningVertical: int = 1
    PixelFormat: str = "BayerRG8"


class StressRecorderShim:
    """Lightweight stand-in for FlirRecorder — provides every attribute that
    RecorderService, encoder_worker, and metadata workers read."""

    def __init__(self, num_cameras: int, fps: float, width: int, height: int, output_dir: str, queue_size: int, segment_frames: int):
        self.cams = [
            FakeCamera(DeviceSerialNumber=f"STRESS_{i:02d}", AcquisitionFrameRate=fps, Width=width, Height=height) for i in range(num_cameras)
        ]
        self.pixel_format = "BayerRG8"

        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.video_root = "stress"
        self.video_base_name = f"stress_{now}"
        self.video_path = output_dir
        self.video_base_file = os.path.join(output_dir, self.video_base_name)

        self.camera_config = {
            "acquisition-type": "continuous" if segment_frames > 0 else "max-frame",
            "acquisition-settings": {
                "image_queue_size": queue_size,
                "video_segment_len": segment_frames if segment_frames > 0 else 0,
                "frame_rate": fps,
                "exposure_time": 15000,
                "nvenc_preset": "auto",
                "chunk_data": [],
            },
            "meta-info": {"system": "stress-test"},
            "camera-info": {cam.DeviceSerialNumber: {} for cam in self.cams},
            "gpio-settings": {"line0": "Off", "line1": "Off", "line2": "Off", "line3": "Off"},
        }
        self.gpio_settings = self.camera_config["gpio-settings"]
        self.config_file = "stress_test"

        self.image_queue_dict: dict = {}
        self.json_queue = None
        self.records_queue = None
        self.writer_error = {"event": threading.Event(), "message": None}
        self.stop_recording = threading.Event()
        self.encoder_stop_event = None
        self.finalize_stop_event = None
        self.finalize_jobs_db = None
        self.preview_callback = None

    def set_status(self, status: str):
        pass

    def set_progress(self, progress: float):
        pass

    @staticmethod
    def get_config_hash(config):
        return "STRESS0000"


# ---------------------------------------------------------------------------
# Background monitor — GPU temp + process RSS
# ---------------------------------------------------------------------------


def _get_gpu_temp() -> int | None:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return int(r.stdout.strip())
    except Exception:
        pass
    return None


def _get_rss_mb() -> float:
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        return ru.ru_maxrss / 1024  # Linux: kB → MB
    except Exception:
        return 0.0


class PipelineMonitor:
    """Background thread that samples GPU temp and process RSS every *interval_s* seconds."""

    def __init__(self, interval_s: float = 10.0):
        self.interval_s = interval_s
        self.gpu_temp_samples: list[tuple[float, int]] = []  # (elapsed_s, temp_c)
        self.rss_samples: list[tuple[float, float]] = []  # (elapsed_s, rss_mb)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0

    def start(self):
        self._t0 = time.monotonic()
        self._sample()  # initial sample
        self._thread = threading.Thread(target=self._run, daemon=True, name="pipeline_monitor")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._sample()  # final sample

    def _sample(self):
        elapsed = time.monotonic() - self._t0
        temp = _get_gpu_temp()
        if temp is not None:
            self.gpu_temp_samples.append((elapsed, temp))
        self.rss_samples.append((elapsed, _get_rss_mb()))

    def _run(self):
        while not self._stop.wait(self.interval_s):
            self._sample()

    @property
    def gpu_max_temp(self) -> int:
        return max((t for _, t in self.gpu_temp_samples), default=0)

    @property
    def gpu_final_temp(self) -> int:
        return self.gpu_temp_samples[-1][1] if self.gpu_temp_samples else 0

    @property
    def gpu_throttled(self) -> bool:
        return self.gpu_max_temp >= 83

    @property
    def rss_growth_rate_mb_per_min(self) -> float:
        if len(self.rss_samples) < 2:
            return 0.0
        first_t, first_rss = self.rss_samples[0]
        last_t, last_rss = self.rss_samples[-1]
        elapsed_min = (last_t - first_t) / 60
        if elapsed_min < 0.5:
            return 0.0
        return (last_rss - first_rss) / elapsed_min


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class StressReport:
    num_cameras: int
    target_fps: float
    duration_s: float
    total_frames_produced: int
    total_frames_expected: int
    dropped_frames: int
    max_queue_depth: dict[str, int]
    actual_fps: float
    wall_time_s: float
    encoder: str
    segments_completed: int
    segment_frames: int
    output_dir: str
    # Segment boundary tracking
    boundary_queue_depths: list[dict[str, int]] = field(default_factory=list)
    # Monitoring
    monitor: PipelineMonitor | None = None


# ---------------------------------------------------------------------------
# Synthetic capture loop
# ---------------------------------------------------------------------------


def run_stress_capture_loop(
    recorder: StressRecorderShim,
    max_frames: int,
    fps: float,
    health: PipelineHealth,
) -> tuple[int, int, dict[str, int], list[dict[str, int]]]:
    """Produce random-noise frames at *fps* and feed the real pipeline.

    Returns (total_frames_produced, segments_completed, max_queue_depth, boundary_depths).
    """
    num_cameras = len(recorder.cams)
    serials = [cam.DeviceSerialNumber for cam in recorder.cams]
    width = recorder.cams[0].Width
    height = recorder.cams[0].Height

    # Pre-allocate one random noise frame per camera (worst-case encoder load).
    noise_frames = {serial: np.random.randint(0, 256, (height, width), dtype=np.uint8) for serial in serials}

    segment_frames = recorder.camera_config["acquisition-settings"].get("video_segment_len", 0)
    is_continuous = recorder.camera_config["acquisition-type"] == "continuous"

    max_queue_depth = {serial: 0 for serial in serials}
    boundary_queue_depths: list[dict[str, int]] = []
    frame_idx = 0
    segment_frame_idx = 0
    segments_completed = 1
    total_frames = segment_frames if is_continuous and segment_frames > 0 else max_frames

    interval = 1.0 / fps
    start_time = time.monotonic()

    prog = tqdm(total=total_frames, desc="Stress test")
    try:
        while True:
            if recorder.stop_recording.is_set():
                break
            if recorder.writer_error["event"].is_set():
                raise RuntimeError(recorder.writer_error["message"] or "worker thread failure")

            if frame_idx >= max_frames:
                break

            # Pace at target FPS
            target_time = start_time + frame_idx * interval
            now = time.monotonic()
            if target_time > now:
                time.sleep(target_time - now)

            real_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            timestamp = int(time.monotonic_ns())

            for serial in serials:
                safe_put(
                    recorder.image_queue_dict[serial],
                    {"im": noise_frames[serial], "real_times": real_time, "timestamps": timestamp, "base_filename": recorder.video_base_file},
                    queue_name=f"image_queue:{serial}",
                    health=health,
                )
                depth = recorder.image_queue_dict[serial].qsize()
                if depth > max_queue_depth[serial]:
                    max_queue_depth[serial] = depth

            metadata = {
                "real_times": real_time,
                "local_times": datetime.now(),
                "base_filename": recorder.video_base_file,
                "timestamps": [timestamp] * num_cameras,
                "frame_id": [segment_frame_idx] * num_cameras,
                "frame_id_abs": [frame_idx] * num_cameras,
                "chunk_serial_data": [-1] * num_cameras,
                "serial_msg": [[]] * num_cameras,
                "camera_serials": serials,
                "exposure_times": [15000.0] * num_cameras,
                "frame_rates_requested": [fps] * num_cameras,
                "frame_rates_binning": [30] * num_cameras,
            }
            put_metadata_or_fail(recorder.json_queue, metadata, timeout_s=2.0, worker_error_state=recorder.writer_error)

            frame_idx += 1
            segment_frame_idx += 1

            if health is not None:
                prog.set_postfix_str(health.format_status())
            prog.update(1)

            # Segment rollover
            if is_continuous and segment_frames > 0 and segment_frame_idx >= segment_frames:
                # Snapshot queue depths at the boundary moment
                boundary_snap = {serial: recorder.image_queue_dict[serial].qsize() for serial in serials}
                boundary_queue_depths.append(boundary_snap)

                segment_frame_idx = 0
                segments_completed += 1
                prog.close()
                prog = tqdm(total=total_frames, desc=f"Segment {segments_completed}")

                now_dt = datetime.now()
                time_str = now_dt.strftime("%Y%m%d_%H%M%S")
                recorder.video_base_name = f"{recorder.video_root}_{time_str}"
                recorder.video_base_file = os.path.join(recorder.video_path, recorder.video_base_name)

    finally:
        prog.close()

    return frame_idx, segments_completed, max_queue_depth, boundary_queue_depths


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_stress_test(
    num_cameras: int = 4,
    fps: float = 30.0,
    duration_s: float = 30.0,
    width: int = 1920,
    height: int = 1200,
    output_dir: str = "/tmp/stress_test",
    queue_size: int = 150,
    segment_frames: int = 0,
) -> StressReport:
    """Run a full pipeline stress test with synthetic worst-case frames."""

    os.makedirs(output_dir, exist_ok=True)

    shim = StressRecorderShim(
        num_cameras=num_cameras, fps=fps, width=width, height=height, output_dir=output_dir, queue_size=queue_size, segment_frames=segment_frames
    )

    setup_recording_logger(output_dir=output_dir, session_name="stress_test")
    health = PipelineHealth(num_cameras=num_cameras)

    max_frames = int(fps * duration_s)

    svc = RecorderService(shim)
    svc.initialize_queues(max_frames=max_frames)

    config_metadata = {
        "meta_info": shim.camera_config["meta-info"],
        "camera_info": shim.camera_config["camera-info"],
        "camera_config_hash": "STRESS0000",
    }

    worker_handles = None
    monitor = PipelineMonitor(interval_s=2.0)
    t0 = time.monotonic()

    try:
        worker_handles = svc.start_workers(config_metadata=config_metadata)
        encoder_name = f"h264_nvenc {worker_handles.preset}" if worker_handles.use_nvenc else f"libx264 {worker_handles.preset}"

        monitor.start()

        total_produced, segments, max_depth, boundary_depths = run_stress_capture_loop(recorder=shim, max_frames=max_frames, fps=fps, health=health)
    finally:
        monitor.stop()
        if worker_handles is not None and worker_handles.writers_started:
            svc.stop_workers(worker_handles)

    wall_time = time.monotonic() - t0
    svc.collect_records()

    return StressReport(
        num_cameras=num_cameras,
        target_fps=fps,
        duration_s=duration_s,
        total_frames_produced=total_produced,
        total_frames_expected=max_frames,
        dropped_frames=health.dropped_frames,
        max_queue_depth=max_depth,
        actual_fps=total_produced / wall_time if wall_time > 0 else 0,
        wall_time_s=wall_time,
        encoder=encoder_name,
        segments_completed=segments,
        segment_frames=segment_frames,
        output_dir=output_dir,
        boundary_queue_depths=boundary_depths,
        monitor=monitor,
    )
