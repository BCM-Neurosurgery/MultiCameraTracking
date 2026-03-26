"""Public FLIR recorder facade used by backend API and CLI entrypoint."""

import PySpin
import simple_pyspin
from simple_pyspin import Camera
from datetime import datetime
from typing import List, Callable, Awaitable, Optional
from pydantic import BaseModel
import concurrent.futures
import logging
import threading
import asyncio
import json
import os
import yaml
import hashlib

from multi_camera.acquisition.flir.camera_control import (
    init_camera as camera_init_camera,
    select_interface as camera_select_interface,
)
from multi_camera.acquisition.flir.camera_runtime import (
    arm_cameras_and_issue_trigger as camera_arm_cameras_and_issue_trigger,
    start_camera_streams as camera_start_camera_streams,
    stop_cameras as camera_stop_cameras,
)
from multi_camera.acquisition.flir.capture_runner import run_capture_loop as capture_run_capture_loop
from multi_camera.acquisition.flir.logging_setup import setup_recording_logger
from multi_camera.acquisition.flir.pipeline.health import PipelineHealth
from multi_camera.acquisition.flir.recorder_service import RecorderService

log = logging.getLogger("flir_pipeline")


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
        self.encode_jobs_db = None
        self.finalize_stop_event = threading.Event()
        self.encode_stop_event = threading.Event()
        self.writer_error = {"event": threading.Event(), "message": None}
        self.config_file = None
        self.iface = None
        self._acquiring = False
        self.status_callback = status_callback
        self.set_status("Uninitialized")

    def get_config_hash(self, yaml_content, hash_len=10):

        # Sorting keys to ensure consistent hashing
        file_str = json.dumps(yaml_content, sort_keys=True)
        encoded_config = file_str.encode("utf-8")

        # Create hash of encoded config file and return
        return hashlib.sha256(encoded_config).hexdigest()[:hash_len]

    def _get_pyspin_system(self):
        # use this to ensure both calls with simple pyspin and locally use the same references
        simple_pyspin.list_cameras()
        self.system = simple_pyspin._SYSTEM  # PySpin.System.GetInstance()

    def get_acquisition_status(self):
        return self.status

    def set_status(self, status):
        log.info("setting status: %s", status)
        self.status = status
        if self.status_callback is not None:
            self.status_callback(status)

    def set_progress(self, progress):
        if self.status_callback is not None:
            self.status_callback(self.status, progress=progress)

    async def synchronize_cameras(self):
        if not all([c.GevIEEE1588 for c in self.cams]):
            self.set_status("Synchronizing")

            log.info("Cameras not synchronized. Enabling IEEE1588 (takes 10 seconds)")
            for c in self.cams:
                c.GevIEEE1588 = True

            await asyncio.sleep(10)

        self.set_status("Synchronized")

    async def configure_cameras(self, config_file: str = None, num_cams: int = None, trigger: bool = True) -> Awaitable[List[CameraStatus]]:
        """
        Configure cameras for recording

        Args:
            config_file (str): Path to config file
            num_cams (int): Number of cameras to configure (if not using config file)
            trigger (bool): Enable network synchronized triggering
        """

        self.config_file = config_file

        iface_list = self.system.GetInterfaces()
        try:
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

            log.info("Requested cameras: %s", requested_cameras)

            # Identify the interface we are going to send a command for synchronous recording
            iface = None
            for i, current_iface in enumerate(iface_list):
                selected_cams = camera_select_interface(current_iface, requested_cameras)

                # If the value returned from select_interface is not None,
                # select the current interface
                if selected_cams is not None:
                    # Break out of the loop after finding the interface and cameras
                    break

            log.info("Using interface %d with %s cameras. In use: %s", i, selected_cams, current_iface.IsInUse())
        finally:
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
            log.info("camera config: %s", self.camera_config)
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

        gamma = self.camera_config.get("acquisition-settings", {}).get("gamma", None)

        config_params = {
            "jumbo_packet": True,
            "triggering": self.trigger,
            "throughput_limit": 125000000,
            "resend_enable": False,
            "binning": binning,
            "exposure_time": exposure_time,
            "frame_rate": frame_rate,
            "gamma": gamma,
            "gpio_settings": self.gpio_settings,
            "chunk_data": self.camera_config["acquisition-settings"]["chunk_data"],
        }

        def _init_or_close(cam):
            try:
                camera_init_camera(cam, **config_params)
            except Exception:
                try:
                    cam.close()
                except Exception:
                    pass
                raise

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.cams)) as executor:
            list(executor.map(_init_or_close, self.cams))

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
            log.info("config hash: %s", config_metadata["camera_config_hash"])
        else:
            config_metadata["meta_info"] = "No Config"
            config_metadata["camera_info"] = [c.DeviceSerialNumber for c in self.cams]
            config_metadata["camera_config_hash"] = None
        return config_metadata

    def _run_capture_loop(self, max_frames: int, health=None):
        return capture_run_capture_loop(recorder=self, max_frames=max_frames, health=health)

    def _shutdown_preview(self):
        if self.preview_callback:
            try:
                self.preview_callback(None)
            except Exception as e:
                log.warning("Preview callback shutdown error: %s", e)

    def start_acquisition(self, recording_path=None, preview_callback: callable = None, max_frames: int = 1000):
        """
        End-to-end acquisition flow:
        1. Prepare recording target/config metadata
        2. Initialize queues and start worker threads
        3. Start + arm cameras
        4. Capture frames and enqueue image/metadata
        5. Stop workers/cameras and collect segment records
        """
        if self._acquiring:
            raise RuntimeError("start_acquisition called while already acquiring")
        self._acquiring = True

        self._prepare_recording_target(recording_path=recording_path, preview_callback=preview_callback)
        config_metadata = self._build_config_metadata()

        # Set up persistent session log alongside the video data.
        health = None
        if self.video_base_file is not None:
            session_name = self.video_base_name
            setup_recording_logger(output_dir=self.video_path, session_name=session_name)
            health = PipelineHealth(num_cameras=len(self.cams))
            log.info("recording started: %s", self.video_base_file)
            log.info(
                "cameras: %d, fps: %s, segment: %s frames",
                len(self.cams),
                self.cams[0].AcquisitionFrameRate if self.cams else "?",
                self.camera_config.get("acquisition-settings", {}).get("video_segment_len", "?"),
            )
            for cam in self.cams:
                log.info("camera %s: %dx%d %s, exposure=%sus", cam.DeviceSerialNumber, cam.Width, cam.Height, cam.PixelFormat, cam.ExposureTime)

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
            camera_start_camera_streams(self.cams)
            cameras_started = True
            camera_arm_cameras_and_issue_trigger(self.cams, self.iface, self.gpio_settings)

            # Phase 3: capture loop.
            self._run_capture_loop(max_frames=max_frames, health=health)
            log.info("Finished recording")

        finally:
            # Phase 4: orderly shutdown.
            self._acquiring = False
            self._shutdown_preview()
            camera_stop_cameras(self.cams, getattr(self, "gpio_settings", {}), cameras_started)
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

        log.info("Reopening and resetting")
        ########## working with new, temporary, reference to PySpin system
        # this seems important for reliability

        # find the set of cameras and trigger a reset on them
        system = PySpin.System.GetInstance()
        cams = system.GetCameras()

        def reset_cam(s):
            log.info("Opening and resetting camera %s", s)
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
                log.info("Closing camera %s", c.DeviceSerialNumber)
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

        log.info("PySpin system released")

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
    parser.add_argument("-p", "--preview", default=False, action="store_true", help="Allow real-time visualization of video")
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
