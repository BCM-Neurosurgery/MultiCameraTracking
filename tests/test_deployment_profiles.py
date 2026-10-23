import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def make_preview(tmp_path, *args, profile=None):
    if profile is not None:
        (tmp_path / ".env").write_text(f"MOCAP_PROFILE={profile}\n")
    env = {key: value for key, value in os.environ.items() if key != "MOCAP_PROFILE"}
    return subprocess.run(
        ["make", "-f", str(ROOT / "Makefile"), "-n", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_existing_station_defaults_to_gpu_and_build(tmp_path):
    result = make_preview(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "-f docker-compose.gpu.yml build" in result.stdout
    assert "--build-arg GIT_DESCRIBE=" in result.stdout


@pytest.mark.parametrize("target", ["build", "run", "validate", "endurance"])
def test_cpu_dotenv_selects_overlay_for_every_target(tmp_path, target):
    result = make_preview(tmp_path, target, profile="cpu")
    assert result.returncode == 0, result.stderr
    assert "-f docker-compose.yml -f docker-compose.cpu.yml" in result.stdout
    assert "docker-compose.gpu.yml" not in result.stdout


def test_command_line_profile_overrides_dotenv(tmp_path):
    result = make_preview(tmp_path, "run", "MOCAP_PROFILE=gpu", profile="cpu")
    assert result.returncode == 0, result.stderr
    assert "-f docker-compose.gpu.yml run" in result.stdout


@pytest.mark.parametrize("value", ["", "auto", "CPU", "cpu gpu"])
def test_invalid_profile_fails_before_docker(tmp_path, value):
    result = make_preview(tmp_path, "run", profile=value)
    assert result.returncode != 0
    assert "MOCAP_PROFILE must be gpu or cpu" in result.stderr
    assert "docker compose" not in result.stdout


def render_compose(tmp_path, *files):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is required to validate deployment configuration")
    envfile = tmp_path / "compose.env"
    envfile.write_text(
        f"REACT_APP_BASE_URL=localhost\nDATA_VOLUME={tmp_path}/data\n" f"CAMERA_CONFIGS={tmp_path}/configs\nDATAJOINT_EXTERNAL={tmp_path}/external\n"
    )
    command = ["docker", "compose", "--project-directory", str(ROOT), "--project-name", "mct-profile-test", "--env-file", str(envfile)]
    for file in files:
        command.extend(["-f", str(file)])
    result = subprocess.run(command + ["config", "--format", "json"], cwd=ROOT, capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout)["services"]["mocap"]


def test_gpu_overlay_preserves_previous_deployment(tmp_path):
    legacy = tmp_path / "legacy.yml"
    legacy.write_text(subprocess.check_output(["git", "show", "e633dfb:docker-compose.yml"], cwd=ROOT, text=True))
    before = render_compose(tmp_path, legacy)
    after = render_compose(tmp_path, ROOT / "docker-compose.yml", ROOT / "docker-compose.gpu.yml")
    assert after["environment"].pop("MOCAP_PROFILE") == "gpu"
    assert after == before


def test_cpu_overlay_has_no_nvidia_requirements_and_keeps_mounts(tmp_path):
    cpu = render_compose(tmp_path, ROOT / "docker-compose.yml", ROOT / "docker-compose.cpu.yml")
    gpu = render_compose(tmp_path, ROOT / "docker-compose.yml", ROOT / "docker-compose.gpu.yml")
    assert "deploy" not in cpu
    assert "NVIDIA_DRIVER_CAPABILITIES" not in cpu["environment"]
    assert cpu["environment"]["FORCE_CPU_ENCODE"] == "1"
    assert cpu["build"]["args"] == {"BASE_IMAGE": "ubuntu:22.04", "INSTALL_ANALYSIS": "false"}
    assert cpu["image"] == "peabody124/mocap-cpu"
    for key in ("volumes", "network_mode", "user", "command", "tty", "stdin_open"):
        assert cpu.get(key) == gpu.get(key)


def test_package_metadata_makes_jax_optional(tmp_path, monkeypatch):
    from email.parser import Parser
    from packaging.requirements import Requirement
    from poetry.core.masonry.api import prepare_metadata_for_build_wheel

    monkeypatch.chdir(ROOT)
    metadata_dir = prepare_metadata_for_build_wheel(str(tmp_path))
    metadata = Parser().parsestr((tmp_path / metadata_dir / "METADATA").read_text())
    requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist", [])]
    base = {req.name for req in requirements if req.marker is None or req.marker.evaluate({"extra": ""})}
    analysis = {req.name for req in requirements if req.marker is None or req.marker.evaluate({"extra": "analysis"})}
    jax = {"jax", "jaxlib", "jaxopt", "jaxlie"}
    assert not base.intersection(jax)
    assert jax.issubset(analysis)
