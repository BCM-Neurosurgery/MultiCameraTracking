"""Camera discovery and one-time hardware configuration for FLIR devices."""

from __future__ import annotations

import PySpin
from simple_pyspin import Camera
from tqdm import tqdm


def select_interface(interface, cameras):
    # This method takes in an interface and list of cameras (if a config
    # file is provided) or number of cameras. It checks if the current
    # interface has cameras and returns a list of valid camera IDs or
    # number of cameras
    print("Update cameras:", interface.UpdateCameras())

    # Check the current interface to see if it has cameras
    interface_cams = interface.GetCameras()
    try:
        # Get the number of cameras on the current interface
        num_interface_cams = interface_cams.GetSize()

        retval = None

        if num_interface_cams > 0:
            # If camera list is passed, confirm all SNs are valid
            if isinstance(cameras, list):
                camera_id_list = []

                for c in cameras:
                    cam = interface_cams.GetBySerial(str(c))
                    if cam.IsValid():
                        camera_id_list.append(str(c))

                    del cam  # must release handle

                # if the camera_ID_list does not contain any valid cameras
                # based on the serial numbers present in the config file
                # return None
                if len(camera_id_list) > 0:
                    # Find any invalid IDs in the config
                    invalid_ids = [c for c in cameras if str(c) not in camera_id_list]

                    if invalid_ids:
                        print(f"The following camera ID(s) from are missing: {invalid_ids} but continuing")

                    retval = camera_id_list

            # If num_cams is passed, confirm it is less than or equal to
            # the size of interface_cams and return the correct num_cams
            if isinstance(cameras, int):
                # if num_cams is larger than the # cameras on current interface,
                # raise an error
                assert cameras <= num_interface_cams, f"num_cams={cameras} but the current interface only has {num_interface_cams} cameras."

                # Otherwise, set num_cams to the # of available cameras
                num_cams = cameras
                print(f"No config file passed. Selecting the first {num_cams} cameras in the list.")

                retval = num_cams
    finally:
        # Always release the camera list handle, even if an assertion fires.
        interface_cams.Clear()

    # If there are no cameras on the interface, return None
    return retval


def init_camera(
    c: Camera,
    jumbo_packet: bool = True,
    triggering: bool = True,
    throughput_limit: int = 125000000,
    resend_enable: bool = False,
    binning: int = 1,
    exposure_time: int = 15000,
    frame_rate: int = 30,
    gpio_settings: dict = {},
    chunk_data: list = [],
):
    """
    Initialize camera with settings for recording.
    """
    # Initialize each available camera
    c.init()

    # Resetting binning to 1 to allow for maximum frame size
    c.BinningHorizontal = 1
    c.BinningVertical = 1

    # Ensuring height and width are set to maximum
    c.Width = c.WidthMax
    c.Height = c.HeightMax

    c.PixelFormat = "BayerRG8"  # BGR8 Mono8

    # Now applying desired binning to maximum frame size
    c.BinningHorizontal = binning
    c.BinningVertical = binning

    # use a fixed exposure time to ensure good synchronization. also want to keep this relatively
    # low to reduce blur while obtaining sufficient light
    c.ExposureAuto = "Off"
    c.ExposureTime = exposure_time

    # set desired frame rate when supported
    try:
        c.AcquisitionFrameRateEnable = True
    except Exception as e:
        tqdm.write(f"Could not enable frame rate control on {c.DeviceSerialNumber}: {e}")

    try:
        if hasattr(c, "AcquisitionFrameRateAuto"):
            c.AcquisitionFrameRateAuto = "Off"
    except Exception:
        pass

    try:
        c.AcquisitionFrameRate = frame_rate
    except Exception as e:
        tqdm.write(f"Could not set frame rate on {c.DeviceSerialNumber}: {e}")

    # let the auto gain match the brightness across images as much as possible
    c.GainAuto = "Continuous"
    # c.Gain = 10

    c.ImageCompressionMode = "Off"  # Lossless might get frame rate up but not working currently
    # c.IspEnable = True  # if trying to adjust the color transformations  this is needed

    if jumbo_packet:
        c.GevSCPSPacketSize = 9000
    else:
        c.GevSCPSPacketSize = 1500

    sn = str(getattr(c, "DeviceSerialNumber", "UNKNOWN"))

    # --- set FPS with a readable error if it fails ---
    try:
        c.AcquisitionFrameRate = frame_rate
    except Exception as e:
        # Try to read the max FPS from the GenICam node (optional, but helps)
        max_fps = None
        try:
            cam_ptr = getattr(c, "cam", None) or getattr(c, "_cam", None)
            if cam_ptr is not None:
                fps_node = PySpin.CFloatPtr(cam_ptr.GetNodeMap().GetNode("AcquisitionFrameRate"))
                max_fps = float(fps_node.GetMax())
        except Exception:
            pass

        raise RuntimeError(
            f"[Camera {sn}] Cannot set FPS to {frame_rate}. "
            f"This usually happens when the camera's Ethernet link is running slow (e.g., negotiated down). "
            f"{'Max FPS currently ~'+str(round(max_fps,3)) if max_fps is not None else ''}\n"
            f"Fix: replug the camera's Ethernet cable on the SWITCH side, or move it to another switch port, "
            f"or swap to a known-good Cat6/Cat6a cable."
        ) from e

    # --- set throughput with a readable error if it fails ---
    try:
        c.DeviceLinkThroughputLimit = throughput_limit
    except PySpin.SpinnakerException as e:
        max_tp = None
        link_guess = None
        try:
            cam_ptr = getattr(c, "cam", None) or getattr(c, "_cam", None)
            if cam_ptr is not None:
                tp_node = PySpin.CIntegerPtr(cam_ptr.GetNodeMap().GetNode("DeviceLinkThroughputLimit"))
                max_tp = int(tp_node.GetMax())
                # Very practical heuristic: 12,500,000 ~= 100 Mbps
                if max_tp <= 13_000_000:
                    link_guess = "likely 100 Mbps"
                elif max_tp <= 140_000_000:
                    link_guess = "likely 1 Gbps"
                else:
                    link_guess = "link speed unknown"
        except Exception:
            pass

        raise RuntimeError(
            f"[Camera {sn}] Camera Ethernet bandwidth is too low ({link_guess or 'unknown'}). "
            f"Requested throughput={throughput_limit}, camera max={max_tp}.\n"
            f"Fix: replug the camera's Ethernet cable on the SWITCH side (wait ~5s), "
            f"then retry. If it repeats, change switch port or swap cable."
        ) from e

    c.GevSCPD = 25000

    line0 = gpio_settings["line0"]
    # line1 = gpio_settings['line1'] line1 currently unused
    line2 = gpio_settings["line2"]
    line3 = gpio_settings["line3"]

    if line2 == "3V3_Enable":
        c.LineSelector = "Line2"
        c.LineMode = "Output"
    else:
        if line2 != "Off":
            print(f"{line2} is not valid for line2. Setting to 'Off'")

    if chunk_data:
        c.ChunkModeActive = True
        for chunk_var in chunk_data:
            c.ChunkSelector = chunk_var
            c.ChunkEnable = True

    if triggering:
        # set up masks for triggering
        c.ActionDeviceKey = 0
        c.ActionGroupKey = 1
        c.ActionGroupMask = 1

        # Check the gpio settings
        if line0 == "ArduinoTrigger":
            c.TriggerMode = "Off"
            c.TriggerSelector = "FrameStart"
            c.TriggerSource = "Line0"
            c.TriggerActivation = "RisingEdge"
            c.TriggerOverlap = "ReadOut"
            c.TriggerMode = "On"
        else:
            if line0 != "Off":
                print(f"{line0} is not valid for line0. Setting to 'Off'")
            c.TriggerMode = "Off"
            c.TriggerSelector = "AcquisitionStart"  # Need to select AcquisitionStart for real time clock
            c.TriggerSource = "Action0"
            c.TriggerMode = "On"

        if line3 == "SerialOn":
            c.SerialPortSelector = "SerialPort0"
            c.SerialPortSource = "Line3"
            c.SerialPortBaudRate = "Baud115200"
            c.SerialPortDataBits = 8
            c.SerialPortStopBits = "Bits1"
            c.SerialPortParity = "None"
        else:
            if line3 != "Off":
                print(f"{line3} is not valid for line3. Setting to 'Off'")
