"""Hot-path frame acquisition loop that dispatches image and metadata queues."""

from __future__ import annotations

from datetime import datetime
import os

import PySpin
from tqdm import tqdm

from multi_camera.acquisition.flir.capture_loop import get_image_with_timeout, is_image_timeout_error
from multi_camera.acquisition.flir.pipeline.queues import put_metadata_or_fail, safe_put


def run_capture_loop(recorder, max_frames: int):
    """
    Hot path:
    - pull frame from each camera
    - enqueue image frame (best-effort)
    - enqueue metadata frame (lossless/fail-fast)
    """
    frame_idx = 0
    acquisition_settings = recorder.camera_config.get("acquisition-settings", {}) if isinstance(recorder.camera_config, dict) else {}
    image_timeout_ms = int(acquisition_settings.get("image_timeout_ms", 1000))
    max_consecutive_timeouts = int(acquisition_settings.get("max_consecutive_timeouts", 30))
    metadata_queue_timeout_s = float(acquisition_settings.get("metadata_queue_timeout_s", 2.0))
    # Cache serials and static camera properties once to avoid per-frame
    # PySpin property reads that throw SpinnakerException on disconnect.
    serial_map = {id(camera): camera.DeviceSerialNumber for camera in recorder.cams}
    camera_props = {}
    for camera in recorder.cams:
        sn = serial_map[id(camera)]
        camera_props[sn] = {
            "exposure_time": camera.ExposureTime,
            "binning_fps": camera.BinningHorizontal * 30,
            "frame_rate": camera.AcquisitionFrameRate,
        }
    timeout_streaks = {sn: 0 for sn in serial_map.values()}

    if recorder.camera_config["acquisition-type"] == "continuous":
        total_frames = recorder.camera_config["acquisition-settings"]["video_segment_len"]
    else:
        total_frames = max_frames

    prog = tqdm(total=total_frames)
    try:
        while recorder.camera_config["acquisition-type"] == "continuous" or frame_idx < max_frames:
            if recorder.writer_error["event"].is_set():
                raise RuntimeError(recorder.writer_error["message"] or "writer thread failure")

            # Get the current real time
            real_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            local_time = datetime.now()

            # Use thread safe checking of semaphore to determine whether to stop recording
            if recorder.stop_recording.is_set():
                recorder.stop_recording.clear()
                print("Stopping recording")
                break

            # for each camera, get current frame and dispatch it
            preview_this_frame = recorder.preview_callback is not None and (frame_idx % 10 == 0)
            real_time_images = [] if preview_this_frame else None

            frame_metadata = {"real_times": real_time, "local_times": local_time, "base_filename": recorder.video_base_file}
            frame_metadata["timestamps"] = []
            frame_metadata["frame_id"] = []
            frame_metadata["frame_id_abs"] = []
            frame_metadata["chunk_serial_data"] = []
            frame_metadata["serial_msg"] = []
            frame_metadata["camera_serials"] = []
            frame_metadata["exposure_times"] = []
            frame_metadata["frame_rates_requested"] = []
            frame_metadata["frame_rates_binning"] = []

            for camera in recorder.cams:
                serial = serial_map[id(camera)]
                try:
                    im_ref = get_image_with_timeout(camera, image_timeout_ms)
                except Exception as exc:
                    if is_image_timeout_error(exc):
                        timeout_streaks[serial] += 1
                        if timeout_streaks[serial] == 1 or timeout_streaks[serial] % 10 == 0:
                            tqdm.write(f"{serial}: image timeout streak {timeout_streaks[serial]} " f"(timeout_ms={image_timeout_ms})")
                        if timeout_streaks[serial] >= max_consecutive_timeouts:
                            raise RuntimeError(f"{serial}: exceeded max consecutive image timeouts " f"({max_consecutive_timeouts})") from exc
                        continue

                    timeout_streaks[serial] += 1
                    if timeout_streaks[serial] == 1 or timeout_streaks[serial] % 10 == 0:
                        tqdm.write(f"{serial}: failed to get image, streak {timeout_streaks[serial]} ({exc})")
                    if timeout_streaks[serial] >= max_consecutive_timeouts:
                        raise RuntimeError(f"{serial}: exceeded max consecutive errors " f"({max_consecutive_timeouts})") from exc
                    continue

                timeout_streaks[serial] = 0

                # Always release image ref regardless of success/failure path.
                try:
                    if im_ref.IsIncomplete():
                        im_stat = im_ref.GetImageStatus()
                        print(f"{serial}: Image incomplete | {PySpin.Image.GetImageStatusDescription(im_stat)}")
                        continue

                    timestamp = im_ref.GetTimeStamp()
                    chunk_data = im_ref.GetChunkData()
                    frame_id = im_ref.GetFrameID()
                    frame_id_abs = chunk_data.GetFrameID()

                    serial_msg = []
                    frame_count = -1
                    if recorder.gpio_settings["line3"] == "SerialOn":
                        # We expect only 5 bytes to be sent.
                        if camera.ChunkSerialDataLength == 5:
                            chunk_serial_data = camera.ChunkSerialData
                            serial_msg = chunk_serial_data
                            split_chunk = [ord(ch) for ch in chunk_serial_data]

                            # Reconstruct counter from chunk serial bytes.
                            frame_count = 0
                            for i, b in enumerate(split_chunk):
                                frame_count |= (b & 0x7F) << (7 * i)

                    frame_metadata["timestamps"].append(timestamp)
                    frame_metadata["frame_id"].append(frame_id)
                    frame_metadata["frame_id_abs"].append(frame_id_abs)
                    frame_metadata["chunk_serial_data"].append(frame_count)
                    frame_metadata["serial_msg"].append(serial_msg)
                    frame_metadata["camera_serials"].append(serial)
                    props = camera_props[serial]
                    frame_metadata["exposure_times"].append(props["exposure_time"])
                    frame_metadata["frame_rates_binning"].append(props["binning_fps"])
                    frame_metadata["frame_rates_requested"].append(props["frame_rate"])

                    try:
                        im = im_ref.GetNDArray().copy()
                        if preview_this_frame:
                            real_time_images.append(im)
                    except Exception as exc:
                        tqdm.write(f"Bad frame from {serial}: {exc}")
                        continue

                    if recorder.video_base_file is not None:
                        # Best-effort video queue.
                        safe_put(
                            recorder.image_queue_dict[serial],
                            {
                                "im": im,
                                "real_times": real_time,
                                "timestamps": timestamp,
                                "base_filename": recorder.video_base_file,
                            },
                            queue_name=f"image_queue:{serial}",
                        )
                finally:
                    im_ref.Release()

            if recorder.video_base_file is not None:
                # Sync-critical metadata queue: lossless + fail-fast.
                put_metadata_or_fail(
                    recorder.json_queue,
                    frame_metadata,
                    timeout_s=metadata_queue_timeout_s,
                    worker_error_state=recorder.writer_error,
                )

            if preview_this_frame:
                recorder.preview_callback(real_time_images)

            # Increment after frame is fully dispatched.
            if recorder.camera_config["acquisition-type"] == "continuous":
                frame_idx += 1
                recorder.set_progress(frame_idx / total_frames)
                prog.update(1)

                # Reset progress and segment filename after each segment.
                if frame_idx >= total_frames:
                    prog.close()
                    prog = tqdm(total=total_frames)
                    frame_idx = 0

                    if recorder.video_base_file is not None:
                        now = datetime.now()
                        time_str = now.strftime("%Y%m%d_%H%M%S")
                        recorder.video_base_name = "_".join([recorder.video_root, time_str])
                        recorder.video_base_file = os.path.join(recorder.video_path, recorder.video_base_name)
            else:
                frame_idx += 1
                recorder.set_progress(frame_idx / max_frames)
                prog.update(1)
    finally:
        try:
            prog.close()
        except Exception:
            pass
