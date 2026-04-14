# MultiCameraTracking

Multi-camera video acquisition and biomechanics analysis system. Captures synchronized video from FLIR BFS-PGE-23S3C GigE cameras with GPU-accelerated encoding, then runs pose estimation, SMPL mesh fitting, and OpenSim export.

Runs in Docker on Ubuntu 22.04 with CUDA 12.2, Python 3.10, and Spinnaker SDK 4.3.

## Features

- **Synchronized multi-camera capture** — IEEE 1588 PTP time sync with GEV action commands across up to 8 FLIR GigE cameras
- **Real-time GPU encoding** — Bayer demosaic and H.264 encoding via ffmpeg (NVENC hardware acceleration with libx264 fallback)
- **Durable pipeline** — SQLite WAL-mode job tracking with claim/retry semantics; automatic segment rotation for continuous recording
- **Web GUI** — React frontend + FastAPI backend with WebSocket live updates for remote-controlled recording
- **Deployment tooling** — One-command GPU, network, and DHCP setup for new machines; built-in stress tests and multi-day endurance validation
- **Analysis pipeline** — Calibration (checkerboard + ChArUco), triangulation, SMPL mesh fitting, and OpenSim TRC export via DataJoint

## Architecture

```
Cameras → capture_loop → image_queues (per-camera) → encode_worker (ffmpeg) → .mp4
                       → metadata_queue             → metadata_writer        → .metadata.jsonl
                                                    → metadata_finalizer     → .json
```

Key modules:

| Module | Description |
|---|---|
| `multi_camera.acquisition.flir` | Multi-threaded producer-consumer capture pipeline |
| `multi_camera.acquisition.stress_test` | Deployment validation with synthetic worst-case frames |
| `multi_camera.acquisition.endurance_test` | Multi-day soak test for 24/7 pipeline confidence |
| `multi_camera.analysis` | Calibration, triangulation, SMPL fitting, biomechanics |
| `multi_camera.datajoint` | DataJoint ORM layer for database-driven pipeline |
| `multi_camera.backend` | FastAPI REST API + WebSocket for live control |
| `multi_camera.validation` | Biomechanics validation framework |

## Deployment Profiles

| Profile | Encoder | Analysis pipeline | When to use |
|---|---|---|---|
| `gpu` (default on hosts with NVIDIA GPU) | `h264_nvenc` (hardware) | Installed — SMPL / OpenSim / biomechanics routes work | Recording + analysis hosts |
| `cpu` | `libx264` (software) | **Not installed** — analysis routes return 500 | Acquisition-only hosts without an NVIDIA GPU |

Profile defaults to `gpu`. On CPU-only hosts, pass `PROFILE=cpu` to every `make` invocation (or use the `*-cpu` convenience targets: `make build-cpu`, `make validate-cpu`, etc.).

**CPU-profile caveats:**
- The React frontend and FastAPI backend boot fine, but these routes return HTTP 500 on call because their analysis dependencies (EasyMocap, nimblephysics, pose_pipeline) are not installed: `/mesh`, `/unannotated_recordings`, `/annotation`, `/smpl_trials`, `/smpl`, `/biomechanics_trials`, `/biomechanics`.
- libx264 at 8 cameras × 30 fps × 1920×1200 needs a reasonably fast host CPU. `make validate` benchmarks presets from `medium` down to `ultrafast` and reports the chosen preset + headroom — if the report picks `ultrafast` with tight headroom, the host is marginal.
- CLI analysis scripts under `multi_camera/analysis/*` are not available on the CPU image.

## Prerequisites

- Linux host (Ubuntu 22.04 tested)
- GPU profile: NVIDIA GPU with driver 570+ and NVENC support; Docker with NVIDIA Container Toolkit
- CPU profile: Docker only
- 10G NIC recommended for 8-camera setups
- FLIR Spinnaker SDK 4.3 (bundled in Docker image)

## Getting Started

### 1. Host setup

Run the setup scripts on a fresh machine (requires root):

```bash
# GPU host: install NVIDIA driver, container toolkit, and NVENC libraries
sudo bash setup_gpu.sh

# CPU host: install Docker only (skips all NVIDIA steps)
sudo bash setup_cpu.sh

# Configure 10G NIC with static IP, DHCP server, jumbo frames
sudo bash setup_network.sh          # auto-detects 10G NIC
sudo bash setup_network.sh enp4s0   # or specify interface
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` to set your paths:

```
NETWORK_INTERFACE=enp4s0
DATA_VOLUME=/home/cameras/data
CAMERA_CONFIGS=/home/cameras/configs
DATAJOINT_EXTERNAL=/mnt/datajoint_external
```

### 3. Build and validate

```bash
# GPU host (default)
make build
make validate

# CPU-only host — pass PROFILE=cpu to every target, or use the *-cpu aliases
make build-cpu       # = make build PROFILE=cpu
make validate-cpu    # = make validate PROFILE=cpu
```

`make build PROFILE=cpu` produces `peabody124/mocap-cpu` using `docker/Dockerfile.cpu`. `make validate` on either profile stress-tests the host under worst-case encoding load and emits a single PASS/WARN/FAIL verdict in `<DATA_VOLUME>/stress_test/<timestamp>/report.{txt,json}`.

### 4. Record

```bash
# Interactive shell
make run

# Inside container:
python -m multi_camera.acquisition.flir_recording_api [-m MAX_FRAMES] [-n NUM_CAMS] [--preview] vid_filename
```

Or start the web GUI (launches both backend and frontend):

```bash
# Inside container:
bash /Mocap/start_acquisition_gui.sh
```

## Testing and Validation

```bash
# Unit tests
pytest tests/

# Deployment stress test (synthetic worst-case frames, verifies all outputs)
make validate                  # 5-minute soak (default)
make validate DURATION=600     # 10-minute soak

# Endurance test (real cameras under maximum load)
make endurance                             # 4-hour default
make endurance ENDURANCE_DURATION=86400    # 24-hour soak
make endurance ENDURANCE_DURATION=691200   # 8-day soak
```

## Calibration

Record calibration data and process:

```bash
python -m multi_camera.acquisition.flir_recording_api [-n NUM_CAMS] calibration
python -m multi_camera.datajoint.calibrate_cameras calibration_<basefile>
```

Supports both standard checkerboard and ChArUco board patterns.

## Analysis Pipeline

The analysis pipeline uses [DataJoint](https://github.com/datajoint) for data management and is driven by table population:

1. **Triangulation** — Insert `CalibratedRecording` entries linking calibrations to recordings, then populate `PersonKeypointReconstruction`
2. **SMPL fitting** — `SMPLReconstruction.populate(key)` fits SMPL meshes to 3D keypoints with temporal smoothing
3. **OpenSim export** — `(SMPLReconstruction & key).export_trc('outfile.trc')` exports TRC files for inverse kinematics

Visualization:

```bash
python apps/visualize.py --smpl FILENAME                          # View SMPL reconstruction
python apps/visualize.py --smpl FILENAME --filter SUBJECT_ID      # Filter to one subject
python apps/visualize.py --smpl --top_down FILENAME               # View top-down results
```

## Configuration

Camera configuration is YAML-based. Example `camera_config.yaml`:

```yaml
camera-info:
  "23336091":
    lens_info: "F1.4/6mm"
  "23336092":
    lens_info: "F1.4/6mm"

acquisition-type: "continuous"  # or "max-frame"

acquisition-settings:
  exposure_time: 15000          # microseconds
  frame_rate: 30
  video_segment_len: 1000       # frames per segment in continuous mode
  chunk_data: ["FrameID", "SerialData"]

gpio-settings:
  line0: "Off"        # Opto-isolated input: "Off" or "ArduinoTrigger"
  line1: "Off"        # Opto-isolated output: "Off" or "ExposureActive"
  line2: "Off"        # Non-isolated I/O: "Off" or "3V3_Enable"
  line3: "Off"        # Non-isolated input: "Off" or "SerialOn"

meta-info:
  system: "Mobile"
  location: "Lab Space 3"
```

## Development

```bash
# Install locally (outside Docker)
pip install -r requirements.txt
pip install -e .

# Format (Black, line-length 150)
black --line-length 150 .

# Run tests
pytest tests/
```

## Project Structure

```
multi_camera/
├── acquisition/
│   ├── flir/                 # Core capture pipeline
│   ├── flir_recording_api.py # CLI entry point
│   ├── stress_test/          # Deployment validation
│   ├── endurance_test/       # Long-running soak tests
│   └── diagnostics/          # Debug tools
├── analysis/                 # Calibration, triangulation, SMPL fitting
├── backend/                  # FastAPI + WebSocket server
├── datajoint/                # DataJoint ORM layer
├── validation/               # Biomechanics validation
├── visualization/            # Pose visualization tools
└── version.py                # Version and git metadata
react_frontend/               # React web GUI
docker/                       # Dockerfile and Spinnaker SDK
scripts/                      # Utility scripts
docs/                         # Bug write-ups and design docs
```

## Credits

- [Aniposelib](https://github.com/lambdaloop/aniposelib) — Bundle adjustment for calibration and triangulation
- [EasyMocap](https://github.com/zju3dv/EasyMocap/) — SMPL mesh fitting
- [Pose2Sim](https://github.com/perfanalytics/pose2sim) — OpenSim export and inverse kinematics models
