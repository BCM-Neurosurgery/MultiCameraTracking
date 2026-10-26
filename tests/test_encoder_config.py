import json
from queue import Queue
import shutil
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from multi_camera.acquisition.flir import recorder_service
from multi_camera.acquisition.flir.recorder_service import RecorderService
from multi_camera.acquisition.flir.workers.encoder_worker import _build_ffmpeg_cmd, encoder_worker


@pytest.fixture
def service(monkeypatch):
    monkeypatch.delenv("FORCE_CPU_ENCODE", raising=False)
    return RecorderService(SimpleNamespace(camera_config={"acquisition-settings": {}}, cams=[object()] * 3))


@pytest.mark.parametrize("force", ["env", "config", "fallback"])
def test_cpu_paths_use_configured_preset_without_benchmark(service, monkeypatch, force):
    service.recorder.camera_config["acquisition-settings"]["x264_preset"] = "superfast"
    detect = Mock(return_value=False)
    benchmark = Mock(side_effect=AssertionError("CPU recording must not benchmark NVENC"))
    monkeypatch.setattr(recorder_service, "detect_nvenc", detect)
    monkeypatch.setattr(recorder_service, "recommend_preset", benchmark)
    if force == "env":
        monkeypatch.setenv("FORCE_CPU_ENCODE", "1")
    elif force == "config":
        service.recorder.camera_config["acquisition-settings"]["nvenc_preset"] = "cpu"
    assert service._detect_encoder() == (False, "superfast")
    if force != "fallback":
        detect.assert_not_called()
    benchmark.assert_not_called()


def test_existing_cpu_config_keeps_veryfast_default(service, monkeypatch):
    monkeypatch.setenv("FORCE_CPU_ENCODE", "1")
    service.recorder.camera_config = None
    assert service._detect_encoder() == (False, "veryfast")


@pytest.mark.parametrize("value", ["auto", "p4", "", None, 2])
def test_invalid_cpu_preset_fails_before_workers_start(service, monkeypatch, value):
    monkeypatch.setenv("FORCE_CPU_ENCODE", "1")
    service.recorder.camera_config["acquisition-settings"]["x264_preset"] = value
    with pytest.raises(ValueError, match="x264_preset"):
        service._detect_encoder()


def test_existing_explicit_gpu_preset_is_preserved(service, monkeypatch):
    service.recorder.camera_config["acquisition-settings"]["nvenc_preset"] = "p4"
    monkeypatch.setattr(recorder_service, "detect_nvenc", lambda: True)
    monkeypatch.setattr(recorder_service, "detect_gpu_info", lambda: None)
    benchmark = Mock(side_effect=AssertionError("Explicit GPU preset must not benchmark"))
    monkeypatch.setattr(recorder_service, "recommend_preset", benchmark)
    assert service._detect_encoder() == (True, "p4")


def test_existing_gpu_auto_selection_is_preserved(service, monkeypatch):
    monkeypatch.setattr(recorder_service, "detect_nvenc", lambda: True)
    monkeypatch.setattr(recorder_service, "detect_gpu_info", lambda: None)
    benchmark = Mock(return_value="p3")
    monkeypatch.setattr(recorder_service, "recommend_preset", benchmark)
    assert service._detect_encoder() == (True, "p3")
    benchmark.assert_called_once_with(num_cameras=3)


def test_gpu_ffmpeg_options_are_preserved():
    command = _build_ffmpeg_cmd("output.mp4", 1920, 1200, 30, "BayerRG8", True, "p4")
    pos = command.index("-c:v")
    assert command[pos : pos + 12] == ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "24", "-b:v", "0", "-pix_fmt", "yuv420p"]


def test_cpu_worker_encodes_rollover_and_exits_after_stop(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for the encoder integration test")
    frames = Queue()
    for segment in ("first", "second"):
        for value in (32, 64, 96):
            frames.put({"base_filename": str(tmp_path / segment), "im": np.full((64, 64), value, dtype=np.uint8)})
    frames.put(None)
    commands = []
    original_popen = subprocess.Popen

    def track_encoder(command, *args, **kwargs):
        if "-c:v" in command:
            commands.append(command)
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", track_encoder)
    errors = {"event": threading.Event(), "message": None}
    flushed = threading.Event()
    worker = threading.Thread(
        target=encoder_worker,
        kwargs={
            "image_queue": frames,
            "serial": "TEST",
            "pixel_format": "BayerRG8",
            "acquisition_fps": 10,
            "width": 64,
            "height": 64,
            "use_nvenc": False,
            "preset": "ultrafast",
            "worker_error_state": errors,
            "stop_event": threading.Event(),
            "flush_done_event": flushed,
        },
        daemon=True,
    )
    worker.start()
    worker.join(timeout=15)
    assert not worker.is_alive(), "CPU worker failed to stop"
    assert flushed.is_set()
    assert not errors["event"].is_set(), errors["message"]
    assert frames.unfinished_tasks == 0
    assert len(commands) == 2
    assert all(command[command.index("-preset") + 1] == "ultrafast" for command in commands)
    for segment in ("first", "second"):
        path = tmp_path / f"{segment}.TEST.mp4"
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                "stream=codec_name,pix_fmt,nb_read_frames,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        stream = json.loads(result.stdout)["streams"][0]
        assert stream["codec_name"] == "h264"
        assert stream["pix_fmt"] == "yuv420p"
        assert int(stream["nb_read_frames"]) == 3
        assert (stream["width"], stream["height"]) == (64, 64)
