# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-camera video acquisition and biomechanics analysis system. Captures synchronized video from FLIR BFS-PGE-23S3C cameras, then runs pose estimation, SMPL mesh fitting, and OpenSim export. Runs in Docker on Python 3.10.

## Build & Run

```bash
# Install
pip install -r requirements.txt && pip install -e .

# Docker build (uses Makefile to detect HOST_UID/HOST_GID)
make build

# Run recording
python -m multi_camera.acquisition.flir_recording_api [-m MAX_FRAMES] [-n NUM_CAMS] [--preview] vid_filename

# Run backend API
python -m multi_camera.backend.fastapi

# Run frontend
cd react_frontend && npm start

# Run tests
pytest tests/

# Format (Black, line-length=150)
black --line-length 150 .
```

## Architecture

### FLIR Acquisition Pipeline (`multi_camera/acquisition/flir/`)

Multi-threaded, queue-based producer-consumer pipeline with durable SQLite job tracking. Designed for real-time 8-camera synchronous video capture.

**Data flow:**
```
Cameras → capture_loop → image_queues (per-camera) → journal_writer → .journal files
                       → metadata_queue             → metadata_writer → .metadata.jsonl

journal files → encode_worker (ffmpeg subprocess) → .mp4
.metadata.jsonl → metadata_finalizer → .json (legacy aggregate format)
```

**Key components:**
- `flir_recording_api.py` — Entry point. `FlirRecorder` class manages async lifecycle
- `camera_control.py` / `camera_runtime.py` — Hardware config (exposure, frame rate, GPIO, IEEE1588 sync, GEV action commands)
- `capture_runner.py` / `capture_loop.py` — Hot-path frame acquisition. Polls cameras, dispatches to queues, tracks timeouts
- `pipeline/queues.py` — `RecorderQueues` dataclass. Image queues use best-effort (drop on full), metadata queue uses fail-fast (RuntimeError on full)
- `recorder_service.py` — `RecorderService` orchestrates all worker threads. 4-phase graceful shutdown with timeouts
- `workers/journal_writer_worker.py` — JPEG-compresses raw Bayer frames, writes length-prefixed binary journal files, detects segment boundaries
- `workers/encode_worker.py` — Reads journals, debayers via OpenCV, pipes RGB to ffmpeg (libx264, CRF=18). Uses `proc.stdin.close()` + `proc.stderr.read()` + `proc.wait()` (NOT `proc.communicate()` — see `tmp_issue_py38_communicate_flush.md`)
- `workers/metadata_workers.py` — Writes per-frame `.metadata.jsonl`, finalizes to aggregate `.json`
- `storage/encode_jobs_repo.py` / `finalize_jobs_repo.py` — SQLite WAL-mode job repos with claim/retry semantics

**Shutdown sequence** (`stop_workers()`): 4 phases — (1) sentinel image queues → journal writers flush, (2) stop encode workers → drain jobs, (3) sentinel metadata queue → metadata writer flush, (4) stop finalizer → drain finalize jobs.

**Error propagation:** Workers set a shared `writer_error` event; capture loop checks it and raises RuntimeError. Worker errors are logged but non-fatal to other workers.

### Other Modules

- `multi_camera/analysis/` — Calibration, triangulation, pose reconstruction, SMPL fitting
- `multi_camera/datajoint/` — DataJoint ORM layer for database-driven pipeline (MySQL on localhost:3306)
- `multi_camera/backend/` — FastAPI REST API + WebSocket for live updates
- `multi_camera/visualization/` — Pose visualization tools
- `multi_camera/validation/` — Biomechanics validation framework

## Key Constraints

- **Python 3.10** in Docker container (base image: `nvidia/cuda:12.2.2-runtime-ubuntu22.04`). Spinnaker SDK 4.3, ffmpeg with NVENC baked into image
- **Black formatter** with line-length=150 (configured in `pyproject.toml`)
- **No pre-commit hooks or CI** currently configured
- Camera config is YAML-based: camera serial mapping, acquisition settings, GPIO trigger config
- Docker runs in `--network host` mode for direct camera Ethernet access

## Known Bug Patterns (see `/docs/`)

The `docs/` directory contains detailed write-ups of fixed bugs in the pipeline. Key recurring themes:
- Queue deadlocks from `queue.join()` blocking on worker errors
- Thread lifecycle issues (zombie threads from stale stop events)
- Segment boundary edge cases in journal writers
- Subprocess flush issues on Python 3.8 (`communicate()` vs manual close+wait)
