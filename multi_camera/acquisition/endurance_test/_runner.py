"""Endurance test runner: real cameras with optional noise injection.

Subclasses FlirRecorder so the full production pipeline (queues, workers,
ffmpeg, SQLite finalize jobs, shutdown sequence) is exercised exactly as
in a real recording — only the pixel data fed to encoders can differ.
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from tqdm import tqdm

from multi_camera.acquisition.flir.capture_loop import get_image_with_timeout, is_image_timeout_error
from multi_camera.acquisition.flir.pipeline.queues import put_metadata_or_fail, safe_put
from multi_camera.acquisition.flir_recording_api import FlirRecorder

log = logging.getLogger("flir_pipeline")

# ---------------------------------------------------------------------------
# Endurance-specific result
# ---------------------------------------------------------------------------


@dataclass
class EnduranceReport:
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
    inject_noise: bool
    timeout_count: int = 0
    boundary_queue_depths: list[dict[str, int]] = field(default_factory=list)
    monitor: EnduranceMonitor | None = None
    # Segment cleanup stats
    segments_verified: int = 0
    segments_verify_failed: int = 0


# ---------------------------------------------------------------------------
# Segment cleanup — keeps disk bounded during long runs
# ---------------------------------------------------------------------------


class SegmentCleaner(threading.Thread):
    """Background thread that verifies and deletes old segment files.

    Identifies segments by their ``.metadata.jsonl`` files.  A segment is
    considered complete when the matching ``.json`` (finalized aggregate)
    also exists.  Keeps the newest *keep_n* complete segments and deletes
    older ones after a quick ffprobe sanity check.
    """

    def __init__(self, output_dir: str, num_cameras: int, keep_n: int = 5, interval_s: float = 300):
        super().__init__(daemon=True, name="segment_cleaner")
        self.output_dir = output_dir
        self.num_cameras = num_cameras
        self.keep_n = keep_n
        self.interval_s = interval_s
        self.stop_event = threading.Event()
        self.verified_count = 0
        self.failed_count = 0

    def run(self):
        while not self.stop_event.wait(self.interval_s):
            self._cleanup_cycle()

    def _cleanup_cycle(self):
        # Identify complete segments by their finalized .json (not .metadata.jsonl)
        json_files = sorted(glob.glob(os.path.join(self.output_dir, "*.json")))
        # Exclude report.json and .metadata.jsonl
        segment_jsons = [f for f in json_files if not f.endswith((".metadata.jsonl", "report.json"))]
        if len(segment_jsons) <= self.keep_n:
            return

        # Oldest segments first, keep the newest keep_n
        to_clean = segment_jsons[: -self.keep_n]
        for json_path in to_clean:
            base = json_path.rsplit(".json", 1)[0]
            mp4s = glob.glob(f"{base}.*.mp4")
            # Quick verify: at least one MP4 passes ffprobe
            ok = self._quick_verify(mp4s)
            if ok:
                self.verified_count += 1
                self._delete_segment(base, mp4s)
            else:
                self.failed_count += 1
                log.warning("segment_cleaner: verification failed for %s, keeping files", os.path.basename(base))

    @staticmethod
    def _quick_verify(mp4s: list[str]) -> bool:
        if not mp4s:
            return False
        # Spot-check first MP4 only (speed over thoroughness for cleanup)
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", mp4s[0]],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0 and r.stdout.strip() != ""
        except Exception:
            return False

    @staticmethod
    def _delete_segment(base: str, mp4s: list[str]):
        for f in mp4s:
            try:
                os.remove(f)
            except OSError:
                pass
        for path in (base + ".json", base + ".metadata.jsonl"):
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Endurance capture loop
# ---------------------------------------------------------------------------


def run_endurance_capture_loop(
    recorder: FlirRecorder,
    max_frames: int,
    health,
    inject_noise: bool,
    noise_pool_size: int,
) -> tuple[int, int, int, dict[str, int], list[dict[str, int]]]:
    """Capture from real cameras with optional noise injection.

    Based on capture_runner.run_capture_loop() but:
    - Injects random noise before enqueue (worst-case encoding)
    - Tracks queue depths at segment boundaries
    - Simplified: no GPIO serial decode, no preview callback

    Returns (frames_produced, segments_completed, timeout_count,
             max_queue_depth, boundary_queue_depths).
    """
    serial_map = {id(cam): cam.DeviceSerialNumber for cam in recorder.cams}
    serials = [serial_map[id(cam)] for cam in recorder.cams]
    num_cameras = len(recorder.cams)
    width = int(recorder.cams[0].Width)
    height = int(recorder.cams[0].Height)

    # Cache static camera properties to avoid per-frame PySpin reads.
    camera_props = {}
    for cam in recorder.cams:
        sn = serial_map[id(cam)]
        camera_props[sn] = {
            "exposure_time": cam.ExposureTime,
            "binning_fps": cam.BinningHorizontal * 30,
            "frame_rate": cam.AcquisitionFrameRate,
        }

    acq_settings = recorder.camera_config.get("acquisition-settings", {}) if isinstance(recorder.camera_config, dict) else {}
    image_timeout_ms = int(acq_settings.get("image_timeout_ms", 1000))
    max_consecutive_timeouts = int(acq_settings.get("max_consecutive_timeouts", 30))
    metadata_queue_timeout_s = float(acq_settings.get("metadata_queue_timeout_s", 2.0))

    # Noise pool: N different random frames per camera to defeat temporal compression.
    noise_pools = None
    if inject_noise:
        noise_pools = {serial: [np.random.randint(0, 256, (height, width), dtype=np.uint8) for _ in range(noise_pool_size)] for serial in serials}

    segment_frames = int(acq_settings.get("video_segment_len", max_frames))
    total_frames = segment_frames

    max_queue_depth = {serial: 0 for serial in serials}
    boundary_queue_depths: list[dict[str, int]] = []
    timeout_streaks = {sn: 0 for sn in serials}
    frame_idx = 0
    segment_frame_idx = 0
    segments_completed = 1
    total_timeout_count = 0
    global_frame_idx = 0

    prog = tqdm(total=total_frames, desc="Endurance segment 1")
    try:
        while True:
            if recorder.writer_error["event"].is_set():
                raise RuntimeError(recorder.writer_error["message"] or "worker thread failure")
            if recorder.stop_recording.is_set():
                recorder.stop_recording.clear()
                log.info("Stopping endurance test (stop_recording set)")
                break
            if global_frame_idx >= max_frames:
                break

            real_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            local_time = datetime.now()

            frame_metadata = {
                "real_times": real_time,
                "local_times": local_time,
                "base_filename": recorder.video_base_file,
                "timestamps": [],
                "frame_id": [],
                "frame_id_abs": [],
                "chunk_serial_data": [],
                "serial_msg": [],
                "camera_serials": [],
                "exposure_times": [],
                "frame_rates_requested": [],
                "frame_rates_binning": [],
            }

            for camera in recorder.cams:
                serial = serial_map[id(camera)]
                try:
                    im_ref = get_image_with_timeout(camera, image_timeout_ms)
                except Exception as exc:
                    timeout_streaks[serial] += 1
                    total_timeout_count += 1
                    if timeout_streaks[serial] == 1 or timeout_streaks[serial] % 10 == 0:
                        log.warning(
                            "%s: timeout streak %d (%s)", serial, timeout_streaks[serial], exc if not is_image_timeout_error(exc) else "timeout"
                        )
                    if timeout_streaks[serial] >= max_consecutive_timeouts:
                        raise RuntimeError(f"{serial}: exceeded max consecutive timeouts ({max_consecutive_timeouts})") from exc
                    continue

                timeout_streaks[serial] = 0

                try:
                    if im_ref.IsIncomplete():
                        log.warning("%s: incomplete frame", serial)
                        continue

                    timestamp = im_ref.GetTimeStamp()
                    frame_id = im_ref.GetFrameID()
                    # Simplified: use frame_id for abs too (no chunk data decode in test)
                    frame_id_abs = frame_id

                    frame_metadata["timestamps"].append(timestamp)
                    frame_metadata["frame_id"].append(frame_id)
                    frame_metadata["frame_id_abs"].append(frame_id_abs)
                    frame_metadata["chunk_serial_data"].append(-1)
                    frame_metadata["serial_msg"].append([])
                    frame_metadata["camera_serials"].append(serial)
                    props = camera_props[serial]
                    frame_metadata["exposure_times"].append(props["exposure_time"])
                    frame_metadata["frame_rates_requested"].append(props["frame_rate"])
                    frame_metadata["frame_rates_binning"].append(props["binning_fps"])

                    im = im_ref.GetNDArray().copy()

                    # Noise injection: replace real pixels with pre-allocated random noise.
                    if noise_pools is not None:
                        im = noise_pools[serial][global_frame_idx % noise_pool_size]

                    if recorder.video_base_file is not None:
                        safe_put(
                            recorder.image_queue_dict[serial],
                            {"im": im, "real_times": real_time, "timestamps": timestamp, "base_filename": recorder.video_base_file},
                            queue_name=f"image_queue:{serial}",
                            health=health,
                        )
                        depth = recorder.image_queue_dict[serial].qsize()
                        if depth > max_queue_depth[serial]:
                            max_queue_depth[serial] = depth
                finally:
                    im_ref.Release()

            # Skip metadata enqueue and frame counting if all cameras timed out.
            if not frame_metadata["camera_serials"]:
                continue

            if recorder.video_base_file is not None:
                put_metadata_or_fail(
                    recorder.json_queue, frame_metadata, timeout_s=metadata_queue_timeout_s, worker_error_state=recorder.writer_error
                )

            global_frame_idx += 1
            frame_idx += 1
            segment_frame_idx += 1

            if health is not None:
                prog.set_postfix_str(health.format_status())
            prog.update(1)

            # Segment rollover
            if segment_frames > 0 and segment_frame_idx >= segment_frames:
                boundary_snap = {serial: recorder.image_queue_dict[serial].qsize() for serial in serials}
                boundary_queue_depths.append(boundary_snap)

                segment_frame_idx = 0
                segments_completed += 1
                prog.close()
                prog = tqdm(total=total_frames, desc=f"Endurance segment {segments_completed}")

                now = datetime.now()
                time_str = now.strftime("%Y%m%d_%H%M%S")
                recorder.video_base_name = f"{recorder.video_root}_{time_str}"
                recorder.video_base_file = os.path.join(recorder.video_path, recorder.video_base_name)
    finally:
        prog.close()

    return global_frame_idx, segments_completed, total_timeout_count, max_queue_depth, boundary_queue_depths


# ---------------------------------------------------------------------------
# EnduranceRecorder
# ---------------------------------------------------------------------------


class EnduranceRecorder(FlirRecorder):
    """FlirRecorder that injects random noise before encoding.

    Overrides only ``_run_capture_loop`` — camera lifecycle, queue/worker
    management, and 4-phase shutdown are all inherited from FlirRecorder.
    """

    def __init__(self, inject_noise: bool = True, noise_pool_size: int = 5):
        super().__init__()
        self.inject_noise = inject_noise
        self.noise_pool_size = noise_pool_size
        self._endurance_result: tuple | None = None
        self._health = None

    def _run_capture_loop(self, max_frames: int, health=None):
        self._health = health
        self._endurance_result = run_endurance_capture_loop(
            recorder=self,
            max_frames=max_frames,
            health=health,
            inject_noise=self.inject_noise,
            noise_pool_size=self.noise_pool_size,
        )
