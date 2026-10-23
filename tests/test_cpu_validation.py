import json
import shutil
import subprocess

import pytest

from multi_camera.acquisition.stress_test import __main__ as validation
from multi_camera.acquisition.stress_test._report import Report
from multi_camera.acquisition.stress_test._runner import run_stress_test


@pytest.mark.parametrize("force", ["env", "config"])
def test_cpu_preflight_does_not_require_nvidia(tmp_path, monkeypatch, force):
    monkeypatch.delenv("FORCE_CPU_ENCODE", raising=False)
    cfg = {"num_cameras": 3, "nvenc_preset": "auto", "x264_preset": "superfast", "queue_size": 150}
    if force == "env":
        monkeypatch.setenv("FORCE_CPU_ENCODE", "1")
    else:
        cfg["nvenc_preset"] = "cpu"

    def unexpected_gpu_check(*args):
        raise AssertionError("CPU preflight must not require NVIDIA hardware")

    for name in ("check_gpu", "check_nvenc", "check_nvenc_concurrent"):
        monkeypatch.setattr(validation, name, unexpected_gpu_check)
    monkeypatch.setattr(validation, "check_ram", lambda: {"total_mb": 32768, "available_mb": 30000})
    monkeypatch.setattr(validation, "check_fd_limits", lambda n: {"soft": 10000, "required": 100, "sufficient": True})
    monkeypatch.setattr(validation, "detect_volume_type", lambda path: "ext4")
    monkeypatch.setattr(validation, "check_disk", lambda path: {"free_gb": 3000})
    monkeypatch.setattr(validation, "benchmark_disk_write", lambda *args, **kwargs: 500)
    monkeypatch.setattr(validation, "benchmark_disk_metadata", lambda *args, **kwargs: 1)
    report = Report()
    validation.run_preflight(report, cfg, str(tmp_path))
    assert not report.issues
    assert report.json_data["checks"]["preflight"]["cpu_only"] is True


def test_validation_reads_cpu_preset_from_camera_config(tmp_path):
    config = tmp_path / "cameras.yaml"
    config.write_text("camera-info: {one: {}}\nacquisition-settings:\n  x264_preset: superfast\n")
    assert validation.load_config(str(config))["x264_preset"] == "superfast"


def test_synthetic_pipeline_uses_cpu_preset_and_finalizes_outputs(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for the pipeline integration test")
    monkeypatch.setenv("FORCE_CPU_ENCODE", "1")
    report = run_stress_test(
        num_cameras=1,
        fps=10,
        duration_s=1,
        width=64,
        height=64,
        output_dir=str(tmp_path),
        queue_size=30,
        x264_preset="superfast",
    )
    assert report.encoder == "libx264 superfast"
    assert report.total_frames_produced == 10
    assert report.dropped_frames == 0
    videos = list(tmp_path.glob("*.mp4"))
    assert len(videos) == 1
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames", "-of", "json", str(videos[0])],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert int(json.loads(probe.stdout)["streams"][0]["nb_read_frames"]) == 10
    journals = list(tmp_path.glob("*.metadata.jsonl"))
    assert len(journals) == 1
    assert len(journals[0].read_text().splitlines()) == 10
    aggregate = tmp_path / (videos[0].name.removesuffix(".STRESS_00.mp4") + ".json")
    assert len(json.loads(aggregate.read_text())["frame_id"]) == 10
