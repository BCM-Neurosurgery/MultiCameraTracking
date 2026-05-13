import logging

import pytest


class FakeIntPtr:
    def __init__(self, value):
        self._value = value

    def GetValue(self):
        return self._value


class FakeNodeMap:
    def __init__(self, node_values):
        self._node_values = node_values

    def GetNode(self, name):
        if name not in self._node_values:
            return None
        return ("node", name)


class FakeCamera:
    def __init__(self, node_values):
        self._nodemap = FakeNodeMap(node_values)

    def GetTLStreamNodeMap(self):
        return self._nodemap


def _factory(values):
    def make(node):
        _, name = node
        v = values[name]
        if v is None:
            raise RuntimeError("not readable")
        return FakeIntPtr(v)

    return make


def _readable(_ptr):
    return True


def _full_cam_values(**overrides):
    from multi_camera.acquisition.flir.pipeline.stream_telemetry import _COUNTER_NAMES, _CONFIG_NAMES

    values = {name: 0 for name in _COUNTER_NAMES}
    values.update({name: 0 for name in _CONFIG_NAMES})
    values.update(overrides)
    return values


def test_baseline_emits_one_line_per_camera_with_known_counters(caplog):
    from multi_camera.acquisition.flir.pipeline.stream_telemetry import StreamTelemetry

    cam_values = _full_cam_values(StreamInputBufferCount=7, StreamAnnouncedBufferCount=8, StreamBufferCountManual=10, StreamBufferCountMax=465)
    cam = FakeCamera(cam_values)
    serial_map = {id(cam): "24253448"}

    tel = StreamTelemetry(
        cameras=[cam],
        serial_map=serial_map,
        int_ptr_factory=_factory(cam_values),
        is_readable=_readable,
    )

    with caplog.at_level(logging.DEBUG, logger="flir_pipeline"):
        tel.log_baseline()

    baseline_msgs = [r.getMessage() for r in caplog.records if "reason=baseline" in r.getMessage()]
    assert len(baseline_msgs) == 1
    msg = baseline_msgs[0]
    assert "24253448" in msg
    assert "in=7" in msg
    assert "ann=8" in msg
    assert "drop=0" in msg
    assert "miss_pkt=0" in msg
    assert "q_xfer=0" in msg

    cfg_msgs = [r.getMessage() for r in caplog.records if "StreamConfig" in r.getMessage()]
    assert len(cfg_msgs) == 1
    assert "StreamBufferCountManual=10" in cfg_msgs[0]
    assert "StreamBufferCountMax=465" in cfg_msgs[0]


def test_missing_node_recorded_once_and_not_retried(caplog):
    from multi_camera.acquisition.flir.pipeline.stream_telemetry import StreamTelemetry

    cam_values = _full_cam_values(StreamInputBufferCount=4, StreamAnnouncedBufferCount=8)
    # Simulate a node not exposed by this producer.
    cam_values.pop("StreamPacketResendRequestTimeoutCount")
    cam = FakeCamera(cam_values)
    serial_map = {id(cam): "24253466"}

    tel = StreamTelemetry(
        cameras=[cam],
        serial_map=serial_map,
        int_ptr_factory=_factory(cam_values),
        is_readable=_readable,
    )

    with caplog.at_level(logging.INFO, logger="flir_pipeline"):
        tel.log_baseline()

    missing_msgs = [r.getMessage() for r in caplog.records if "missing=" in r.getMessage()]
    assert len(missing_msgs) == 1
    assert "StreamPacketResendRequestTimeoutCount" in missing_msgs[0]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="flir_pipeline"):
        tel.log_snapshot("24253466", reason="tick", level=logging.INFO)

    snap_msgs = [r.getMessage() for r in caplog.records if "reason=tick" in r.getMessage()]
    assert len(snap_msgs) == 1
    assert "resend_to=" not in snap_msgs[0]
    assert "in=4" in snap_msgs[0]


def test_poll_returns_none_for_failed_reads(caplog):
    from multi_camera.acquisition.flir.pipeline.stream_telemetry import StreamTelemetry

    cam_values = _full_cam_values(StreamInputBufferCount=1, StreamOutputBufferCount=1, StreamAnnouncedBufferCount=8)
    cam = FakeCamera(cam_values)
    serial_map = {id(cam): "24253450"}
    tel = StreamTelemetry(
        cameras=[cam],
        serial_map=serial_map,
        int_ptr_factory=_factory(cam_values),
        is_readable=_readable,
    )

    class ExplodingPtr:
        def GetValue(self):
            raise RuntimeError("disconnected")

    tel._handles["24253450"]["StreamInputBufferCount"] = ExplodingPtr()

    stats = tel.poll("24253450")
    assert stats["StreamInputBufferCount"] is None
    assert stats["StreamOutputBufferCount"] == 1


def test_log_snapshot_skips_unknown_serial(caplog):
    from multi_camera.acquisition.flir.pipeline.stream_telemetry import StreamTelemetry

    tel = StreamTelemetry(
        cameras=[],
        serial_map={},
        int_ptr_factory=_factory({}),
        is_readable=_readable,
    )
    with caplog.at_level(logging.WARNING, logger="flir_pipeline"):
        tel.log_snapshot("nonexistent", reason="streak1")
    assert not [r for r in caplog.records if "nonexistent" in r.getMessage()]
