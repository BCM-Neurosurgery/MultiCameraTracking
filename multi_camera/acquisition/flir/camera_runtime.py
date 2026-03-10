"""Runtime camera lifecycle helpers: start streams, arm trigger, and stop."""

from __future__ import annotations

import concurrent.futures
import logging

log = logging.getLogger("flir_pipeline")


def start_camera_streams(cams):
    """Start camera acquisition streams concurrently."""

    def start_cam(i):
        # This won't truly start cameras until trigger command is sent.
        cams[i].start()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cams)) as executor:
        list(executor.map(start_cam, range(len(cams))))


def arm_cameras_and_issue_trigger(cams, iface, gpio_settings: dict):
    """Apply runtime GPIO setup and issue synchronized action trigger."""
    log.info("Acquisition, Resulting, Exposure, DeviceLinkThroughputLimit:")
    for camera in cams:
        log.info(
            "%s: %s, %s, %s, %s",
            camera.DeviceSerialNumber,
            camera.AcquisitionFrameRate,
            camera.AcquisitionResultingFrameRate,
            camera.ExposureTime,
            camera.DeviceLinkThroughputLimit,
        )
        log.info("Frame Size: %s %s", camera.Width, camera.Height)

        if gpio_settings.get("line2") == "3V3_Enable":
            camera.LineSelector = "Line2"
            camera.LineMode = "Input"
            camera.V3_3Enable = True
        if gpio_settings.get("line3") == "SerialOn":
            log.debug(
                "SerialReceiveQueue current=%s max=%s", camera.SerialReceiveQueueCurrentCharacterCount, camera.SerialReceiveQueueMaxCharacterCount
            )
            camera.SerialReceiveQueueClear()
            log.debug("SerialReceiveQueue after clear=%s", camera.SerialReceiveQueueCurrentCharacterCount)

    # Schedule action command ~250ms in the future.
    cams[0].TimestampLatch()
    value = cams[0].TimestampLatchValue
    latch_value = int(value + 0.250 * 1e9)
    iface.TLInterface.GevActionTime.SetValue(latch_value)
    iface.TLInterface.GevActionGroupKey.SetValue(1)  # these group/mask/device numbers should match above
    iface.TLInterface.GevActionGroupMask.SetValue(1)
    iface.TLInterface.GevActionDeviceKey.SetValue(0)
    iface.TLInterface.ActionCommand()


def stop_cameras(cams, gpio_settings: dict, cameras_started: bool):
    """Stop camera streams and restore runtime GPIO state."""
    for camera in cams:
        if gpio_settings.get("line2") == "3V3_Enable":
            try:
                camera.LineSelector = "Line2"
                camera.V3_3Enable = False
                camera.LineMode = "Output"
            except Exception as exc:
                log.warning("Failed to disable 3V3 on %s: %s", camera.DeviceSerialNumber, exc)

        if cameras_started:
            try:
                camera.stop()
            except Exception as exc:
                log.warning("Failed to stop camera %s: %s", camera.DeviceSerialNumber, exc)
