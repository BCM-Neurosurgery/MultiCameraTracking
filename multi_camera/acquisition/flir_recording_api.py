import PySpin
import simple_pyspin
from simple_pyspin import Camera
from tqdm import tqdm
from datetime import datetime
from typing import List, Callable, Awaitable, Optional
from pydantic import BaseModel
import concurrent.futures
import threading
import asyncio
import json
import os
import yaml
import hashlib

from multi_camera.acquisition.flir.pipeline.queues import (
    put_metadata_or_fail as queue_put_metadata_or_fail,
    safe_put as queue_safe_put,
)
from multi_camera.acquisition.flir.capture_loop import (
    get_image_with_timeout as capture_get_image_with_timeout,
    is_image_timeout_error as capture_is_image_timeout_error,
)
from multi_camera.acquisition.flir.camera_control import (
    init_camera as camera_init_camera,
    select_interface as camera_select_interface,
)
from multi_camera.acquisition.flir.recorder_service import RecorderService


# Data structures we expose outside this library
class CameraStatus(BaseModel):
    # This contains the information from init_camera
    SerialNumber: str
    Status: str = "Not Initialized"
    # PixelSize: float = 0.0
    PixelFormat: str = ""
    BinningHorizontal: int = 0
    BinningVertical: int = 0
    Width: int = 0
    Height: int = 0
    SyncOffset: float = 0.0

class FlirRecorder:
    def __init__(
        self,
        status_callback: Callable[[str], None] = None,
    ):
        self._get_pyspin_system()

        # Set up thread safe semaphore to stop recording from a different thread
        self.stop_recording = threading.Event()

        self.preview_callback = None
        self.cams = []
        self.image_queue_dict = {}
        self.json_queue = None
        self.finalize_jobs_db = None
        self.finalize_stop_event = threading.Event()
        self.writer_error = {"event": threading.Event(), "message": None}
        self.config_file = None
        self.iface = None
        self.status_callback = status_callback
        self.set_status("Uninitialized")

    def get_config_hash(self,yaml_content,hash_len=10):

        # Sorting keys to ensure consistent hashing
        file_str = json.dumps(yaml_content,sort_keys=True)
        encoded_config = file_str.encode('utf-8')

        # Create hash of encoded config file and return
        return hashlib.sha256(encoded_config).hexdigest()[:hash_len]

    def _get_pyspin_system(self):
        # use this to ensure both calls with simple pyspin and locally use the same references
        simple_pyspin.list_cameras()
        self.system = simple_pyspin._SYSTEM  # PySpin.System.GetInstance()

    def get_acquisition_status(self):
        return self.status

    def set_status(self, status):
        print("setting status: ", status)
        self.status = status
        if self.status_callback is not None:
            self.status_callback(status)

    def set_progress(self, progress):
        if self.status_callback is not None:
            self.status_callback(self.status, progress=progress)

    async def synchronize_cameras(self):
        if not all([c.GevIEEE1588 for c in self.cams]):
            self.set_status("Synchronizing")

            print("Cameras not synchronized. Enabling IEEE1588 (takes 10 seconds)")
            for c in self.cams:
                c.GevIEEE1588 = True

            await asyncio.sleep(10)

        self.set_status("Synchronized")

    async def configure_cameras(
        self, config_file: str = None, num_cams: int = None, trigger: bool = True
    ) -> Awaitable[List[CameraStatus]]:
        """
        Configure cameras for recording

        Args:
            config_file (str): Path to config file
            num_cams (int): Number of cameras to configure (if not using config file)
            trigger (bool): Enable network synchronized triggering
        """

        self.config_file = config_file

        iface_list = self.system.GetInterfaces()

        if config_file:
            with open(config_file, "r") as file:
                self.camera_config = yaml.safe_load(file)

            # Updating interface_cameras if a config file is passed
            # with the camera IDs passed
            requested_cameras = list(self.camera_config["camera-info"].keys())
        else:
            assert num_cams is not None, "Must provide number of cameras if no config file is provided"
            requested_cameras = num_cams
            self.camera_config = {}

        print(f"Requested cameras: {requested_cameras}")

        # Identify the interface we are going to send a command for synchronous recording
        iface = None
        for i, current_iface in enumerate(iface_list):
            selected_cams = camera_select_interface(current_iface, requested_cameras)

            # If the value returned from select_interface is not None,
            # select the current interface
            if selected_cams is not None:
                # Break out of the loop after finding the interface and cameras
                break

        print(f"Using interface {i} with {selected_cams} cameras. In use: {current_iface.IsInUse()}")

        iface_list.Clear()

        # Confirm that cameras were found on an interface
        assert current_iface is not None, "Unable to find valid interface."
        self.iface = current_iface
        self.iface_cameras = selected_cams

        self.trigger = trigger

        self.iface.TLInterface.GevActionDeviceKey.SetValue(0)
        self.iface.TLInterface.GevActionGroupKey.SetValue(1)
        self.iface.TLInterface.GevActionGroupMask.SetValue(1)

        if type(self.iface_cameras) is int:
            self.cams = [Camera(i, lock=True) for i in range(self.iface_cameras)]
        else:
            # if config is passed then use the config list
            # of cameras to select
            self.cams = [Camera(i, lock=True) for i in self.iface_cameras]

        if self.camera_config:
            print(self.camera_config)
            # Parse additional parameters from the config file
            exposure_time = self.camera_config["acquisition-settings"]["exposure_time"]
            frame_rate = self.camera_config["acquisition-settings"]["frame_rate"]
            self.gpio_settings = self.camera_config["gpio-settings"]

        else:
            # If no config file is passed, use default values
            exposure_time = 15000
            frame_rate = 30

        # Updating the binning needed to run at 60 Hz. 
        # TODO: make this check more robust in the future
        if frame_rate == 60:
            binning = 2
        else:
            binning = 1

        config_params = {
            "jumbo_packet": True,
            "triggering": self.trigger,
            "throughput_limit": 125000000,
            "resend_enable": False,
            "binning": binning,
            "exposure_time": exposure_time,
            "frame_rate": frame_rate,
            "gpio_settings": self.gpio_settings,
            "chunk_data": self.camera_config["acquisition-settings"]["chunk_data"]
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.cams)) as executor:
            list(executor.map(lambda c: camera_init_camera(c, **config_params), self.cams))

        await self.synchronize_cameras()

        self.cams.sort(key=lambda x: x.DeviceSerialNumber)
        self.pixel_format = self.cams[0].PixelFormat

        self.set_status("Idle")

    async def get_camera_status(self) -> List[CameraStatus]:
        status = [
            CameraStatus(
                SerialNumber=c.DeviceSerialNumber,
                Status="Initialized",
                # PixelSize=c.PixelSize,
                PixelFormat=c.PixelFormat,
                BinningHorizontal=c.BinningHorizontal,
                BinningVertical=c.BinningVertical,
                Width=c.Width,
                Height=c.Height,
            )
            for c in self.cams
        ]

        for c in self.cams:
            c.GevIEEE1588DataSetLatch()
            # print(
            #    "Primary" if c.GevIEEE1588StatusLatched == "Master" else "Secondary",
            #    c.GevIEEE1588OffsetFromMasterLatched,
            # )

            # set the corresponding camera status
            for cs in status:
                if cs.SerialNumber == c.DeviceSerialNumber:
                    cs.SyncOffset = c.GevIEEE1588OffsetFromMasterLatched

        status.sort(key=lambda x: x.SerialNumber)

        return status

    def _prepare_recording_target(self, recording_path: Optional[str], preview_callback: callable = None):
        self.set_status("Recording")
        self.preview_callback = preview_callback
        self.video_base_file = recording_path

        if self.video_base_file is not None:
            self.video_base_name = self.video_base_file.split("/")[-1]
            self.video_path = "/".join(self.video_base_file.split("/")[:-1])

            # Split the video_base_name to get the root and the date
            self.video_root = "_".join(self.video_base_name.split("_")[:-2])

    def _build_config_metadata(self) -> dict:
        config_metadata = {}
        if self.camera_config:
            config_metadata["meta_info"] = self.camera_config["meta-info"]
            config_metadata["camera_info"] = self.camera_config["camera-info"]
            config_metadata["camera_config_hash"] = self.get_config_hash(self.camera_config)
            print("CONFIG HASH", config_metadata["camera_config_hash"])
        else:
            config_metadata["meta_info"] = "No Config"
            config_metadata["camera_info"] = [c.DeviceSerialNumber for c in self.cams]
            config_metadata["camera_config_hash"] = None
        return config_metadata

    def _start_camera_streams(self):
        def start_cam(i):
            # this won't truly start them until command is sent below
            self.cams[i].start()

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.cams)) as executor:
            list(executor.map(start_cam, range(len(self.cams))))

    def _arm_cameras_and_issue_trigger(self):
        print("Acquisition, Resulting, Exposure, DeviceLinkThroughputLimit:")
        for c in self.cams:
            print(f"{c.DeviceSerialNumber}: {c.AcquisitionFrameRate}, {c.AcquisitionResultingFrameRate}, {c.ExposureTime}, {c.DeviceLinkThroughputLimit} ")
            print(f"Frame Size: {c.Width} {c.Height}")

            if self.gpio_settings['line2'] == '3V3_Enable':
                c.LineSelector = 'Line2'
                c.LineMode = 'Input'
                c.V3_3Enable = True
            if self.gpio_settings['line3'] == 'SerialOn':
                print(c.SerialReceiveQueueCurrentCharacterCount)
                print(c.SerialReceiveQueueMaxCharacterCount)
                c.SerialReceiveQueueClear()
                print(c.SerialReceiveQueueCurrentCharacterCount)

        # schedule a command to start in 250 ms in the future
        self.cams[0].TimestampLatch()
        value = self.cams[0].TimestampLatchValue
        latch_value = int(value + 0.250 * 1e9)
        self.iface.TLInterface.GevActionTime.SetValue(latch_value)
        self.iface.TLInterface.GevActionGroupKey.SetValue(1)  # these group/mask/device numbers should match above
        self.iface.TLInterface.GevActionGroupMask.SetValue(1)
        self.iface.TLInterface.GevActionDeviceKey.SetValue(0)
        self.iface.TLInterface.ActionCommand()

    def _run_capture_loop(self, max_frames: int):
        """
        Hot path:
        - pull frame from each camera
        - enqueue image frame (best-effort)
        - enqueue metadata frame (lossless/fail-fast)
        """
        frame_idx = 0
        acquisition_settings = self.camera_config.get("acquisition-settings", {}) if isinstance(self.camera_config, dict) else {}
        image_timeout_ms = int(acquisition_settings.get("image_timeout_ms", 1000))
        max_consecutive_timeouts = int(acquisition_settings.get("max_consecutive_timeouts", 30))
        metadata_queue_timeout_s = float(acquisition_settings.get("metadata_queue_timeout_s", 2.0))
        timeout_streaks = {c.DeviceSerialNumber: 0 for c in self.cams}

        if self.camera_config["acquisition-type"] == "continuous":
            total_frames = self.camera_config["acquisition-settings"]["video_segment_len"]
        else:
            total_frames = max_frames

        prog = tqdm(total=total_frames)
        try:
            while self.camera_config["acquisition-type"] == "continuous" or frame_idx < max_frames:
                if self.writer_error["event"].is_set():
                    raise RuntimeError(self.writer_error["message"] or "writer thread failure")

                # Get the current real time
                real_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                local_time = datetime.now()

                # Use thread safe checking of semaphore to determine whether to stop recording
                if self.stop_recording.is_set():
                    self.stop_recording.clear()
                    print("Stopping recording")
                    break

                if self.camera_config["acquisition-type"] == "continuous":
                    self.set_progress(frame_idx / total_frames)
                    prog.update(1)

                    # Reset progress and segment filename after each segment.
                    if frame_idx % total_frames == 0:
                        prog = tqdm(total=total_frames)
                        frame_idx = 0

                        if self.video_base_file is not None:
                            now = datetime.now()
                            time_str = now.strftime("%Y%m%d_%H%M%S")
                            self.video_base_name = "_".join([self.video_root, time_str])
                            self.video_base_file = os.path.join(self.video_path, self.video_base_name)
                else:
                    self.set_progress(frame_idx / max_frames)
                    prog.update(1)

                # for each camera, get current frame and dispatch it
                preview_this_frame = self.preview_callback is not None and (frame_idx % 10 == 0)
                real_time_images = [] if preview_this_frame else None

                frame_metadata = {"real_times": real_time, "local_times": local_time, "base_filename": self.video_base_file}
                frame_metadata["timestamps"] = []
                frame_metadata["frame_id"] = []
                frame_metadata["frame_id_abs"] = []
                frame_metadata["chunk_serial_data"] = []
                frame_metadata["serial_msg"] = []
                frame_metadata["camera_serials"] = []
                frame_metadata["exposure_times"] = []
                frame_metadata["frame_rates_requested"] = []
                frame_metadata["frame_rates_binning"] = []

                for c in self.cams:
                    serial = c.DeviceSerialNumber
                    try:
                        im_ref = capture_get_image_with_timeout(c, image_timeout_ms)
                    except Exception as e:
                        if capture_is_image_timeout_error(e):
                            timeout_streaks[serial] += 1
                            if timeout_streaks[serial] == 1 or timeout_streaks[serial] % 10 == 0:
                                tqdm.write(
                                    f"{serial}: image timeout streak {timeout_streaks[serial]} "
                                    f"(timeout_ms={image_timeout_ms})"
                                )
                            if timeout_streaks[serial] >= max_consecutive_timeouts:
                                raise RuntimeError(
                                    f"{serial}: exceeded max consecutive image timeouts "
                                    f"({max_consecutive_timeouts})"
                                ) from e
                            continue

                        tqdm.write(f"{serial}: failed to get image ({e})")
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
                        if self.gpio_settings['line3'] == 'SerialOn':
                            # We expect only 5 bytes to be sent.
                            if c.ChunkSerialDataLength == 5:
                                chunk_serial_data = c.ChunkSerialData
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
                        frame_metadata["exposure_times"].append(c.ExposureTime)
                        frame_metadata["frame_rates_binning"].append(c.BinningHorizontal * 30)
                        frame_metadata["frame_rates_requested"].append(c.AcquisitionFrameRate)

                        try:
                            im = im_ref.GetNDArray()
                            if preview_this_frame:
                                real_time_images.append(im)
                        except Exception as e:
                            tqdm.write(f"Bad frame from {serial}: {e}")
                            continue

                        if self.video_base_file is not None:
                            # Best-effort video queue.
                            queue_safe_put(
                                self.image_queue_dict[serial],
                                {
                                    "im": im,
                                    "real_times": real_time,
                                    "timestamps": timestamp,
                                    "base_filename": self.video_base_file,
                                },
                                queue_name=f"image_queue:{serial}",
                            )
                    finally:
                        im_ref.Release()

                if self.video_base_file is not None:
                    # Sync-critical metadata queue: lossless + fail-fast.
                    queue_put_metadata_or_fail(
                        self.json_queue,
                        frame_metadata,
                        timeout_s=metadata_queue_timeout_s,
                        worker_error_state=self.writer_error,
                    )

                if preview_this_frame:
                    self.preview_callback(real_time_images)

                frame_idx += 1
        finally:
            try:
                prog.close()
            except Exception:
                pass

    def _shutdown_preview(self):
        if self.preview_callback:
            try:
                self.preview_callback(None)
            except Exception as e:
                tqdm.write(f"Preview callback shutdown error: {e}")

    def _stop_cameras(self, cameras_started: bool):
        for c in self.cams:
            if getattr(self, "gpio_settings", {}).get('line2') == '3V3_Enable':
                try:
                    c.LineSelector = 'Line2'
                    c.V3_3Enable = False
                    c.LineMode = 'Output'
                except Exception as e:
                    tqdm.write(f"Failed to disable 3V3 on {c.DeviceSerialNumber}: {e}")

            if cameras_started:
                try:
                    c.stop()
                except Exception as e:
                    tqdm.write(f"Failed to stop camera {c.DeviceSerialNumber}: {e}")

    def start_acquisition(self, recording_path=None, preview_callback: callable = None, max_frames: int = 1000):
        """
        End-to-end acquisition flow:
        1. Prepare recording target/config metadata
        2. Initialize queues and start worker threads
        3. Start + arm cameras
        4. Capture frames and enqueue image/metadata
        5. Stop workers/cameras and collect segment records
        """
        self._prepare_recording_target(recording_path=recording_path, preview_callback=preview_callback)
        config_metadata = self._build_config_metadata()
        recorder_service = RecorderService(self)
        recorder_service.initialize_queues(max_frames=max_frames)
        self.writer_error["event"].clear()
        self.writer_error["message"] = None
        cameras_started = False
        worker_handles = None

        try:
            # Phase 1: start queue-owned workers.
            worker_handles = recorder_service.start_workers(config_metadata=config_metadata)
            # Phase 2: start + arm cameras.
            self._start_camera_streams()
            cameras_started = True
            self._arm_cameras_and_issue_trigger()

            # Phase 3: capture loop.
            self._run_capture_loop(max_frames=max_frames)
            print("Finished recording")

        finally:
            # Phase 4: orderly shutdown.
            self._shutdown_preview()
            self._stop_cameras(cameras_started=cameras_started)
            if worker_handles is not None and worker_handles.writers_started:
                recorder_service.stop_workers(worker_handles)
            records = recorder_service.collect_records()

            if self.writer_error["event"].is_set():
                raise RuntimeError(self.writer_error["message"] or "writer thread failure")

            self.set_status("Idle")

        return records

    def stop_acquisition(self):
        self.stop_recording.set()

    async def reset_cameras(self):
        """Reset all the cameras and reopen the system"""

        self.set_status("Resetting")
        await asyncio.sleep(0.1)  # let the web service update with this message

        # store the serial numbers to get and reset
        serials = [c.DeviceSerialNumber for c in self.cams]
        config_file = self.config_file  # grab this before closing as it is cleared

        # this releases all the handles to the pyspin system.
        self.close()

        print("Reopening and resetting")
        ########## working with new, temporary, reference to PySpin system
        # this seems important for reliability

        # find the set of cameras and trigger a reset on them
        system = PySpin.System.GetInstance()
        cams = system.GetCameras()

        def reset_cam(s):
            print("Opening and resetting camera", s)
            c = cams.GetBySerial(s)
            c.Init()
            c.DeviceReset()
            c.DeInit()
            del c  # force release of the handle

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(serials)) as executor:
            executor.map(reset_cam, serials)

        cams.Clear()
        system.ReleaseInstance()

        ########## go back to the original reference to the PySpin system

        self.set_status("Reset complete. Waiting to reconfigure.")
        await asyncio.sleep(15)

        # set up the PySpin system reference again
        self._get_pyspin_system()

        if config_file is not None and config_file != "":
            await self.configure_cameras(config_file)

    def close(self):
        """Close all the cameras and release the system"""

        if len(self.cams) > 0:

            def close_cam(c):
                print("Closing camera", c.DeviceSerialNumber)
                c.cam.DeInit()
                c.close()
                del c

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.cams)) as executor:
                executor.map(close_cam, self.cams)

        self.cams = []

        if self.iface is not None:
            del self.iface
            self.iface = None

        simple_pyspin._SYSTEM.ReleaseInstance()
        del simple_pyspin._SYSTEM
        simple_pyspin._SYSTEM = None

        self.system = None

        self.config_file = None

        print("PySpin system released")

    def reset(self):
        self.close()
        self._get_pyspin_system()


if __name__ == "__main__":
    import argparse
    import signal

    parser = argparse.ArgumentParser(description="Record video from GigE FLIR cameras")
    parser.add_argument("vid_file", help="Video file to write")
    parser.add_argument("-m", "--max_frames", type=int, default=1000, help="Maximum frames to record")
    parser.add_argument("-n", "--num_cams", type=int, default=4, help="Number of input cameras")
    parser.add_argument("-r", "--reset", default=False, action="store_true", help="Reset cameras first")
    parser.add_argument(
        "-p", "--preview", default=False, action="store_true", help="Allow real-time visualization of video"
    )
    parser.add_argument(
        "-s",
        "--scaling",
        type=float,
        default=0.5,
        help="Ratio to use for scaling the real-time visualization output (should be a float between 0 and 1)",
    )
    parser.add_argument("-c", "--config", default="", type=str, help="Path to a config.yaml file")
    args = parser.parse_args()

    print(args.config)
    acquisition = FlirRecorder()
    asyncio.run(acquisition.configure_cameras(config_file=args.config, num_cams=args.num_cams))

    print(asyncio.run(acquisition.get_camera_status()))

    if args.reset:
        print("reset")
        asyncio.run(acquisition.reset_cameras())

    # time.sleep(5)

    # Get the timestamp that should be used for the file names
    now = datetime.now()
    time_str = now.strftime("%Y%m%d_%H%M%S")
    filename = f"{args.vid_file}_{time_str}.mp4"

    # install a signal handler to call stop acquisition on Ctrl-C
    signal.signal(signal.SIGINT, lambda sig, frame: acquisition.stop_acquisition())

    # loop = asyncio.get_event_loop()
    # loop.run_until_complete(acquisition.start_acquisition(recording_path=filename, max_frames=args.max_frames))
    acquisition.start_acquisition(recording_path=filename, max_frames=args.max_frames)

    acquisition.close()
