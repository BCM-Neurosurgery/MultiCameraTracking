# Endurance Test

Proves the recording pipeline can run 24/7 under worst-case load with real cameras connected. Complements `make validate` (synthetic frames, no cameras) and the QC assistant (real cameras, 20 seconds).

## Quick Start

```bash
make endurance                             # 4-hour soak (default)
make endurance ENDURANCE_DURATION=86400    # 24-hour soak
make endurance ENDURANCE_DURATION=691200   # 8-day soak (max patient stay)
```

## Why This Exists

| Test | Real Cameras | Worst-Case Encoding | Extended Duration |
|------|:---:|:---:|:---:|
| `make validate` | no | yes (random noise) | 5 min |
| QC assistant | yes | no (ambient scene) | 20 sec |
| **`make endurance`** | **yes** | **yes** | **hours to days** |

An empty room compresses trivially and never stresses the encoder. Active patients generate high-entropy frames that push GPU encoding to its limits. This test captures real frames from cameras, then replaces the pixel data with random noise before encoding — exercising both the camera path (GigE, PTP, firmware) and the encoding path (worst-case GPU load) simultaneously.

## What It Tests

### Phase 1: Preflight
Same hardware checks as `make validate` (GPU, NVENC, RAM, disk I/O, file descriptors).

### Phase 2: Camera Initialization
- Discovers and configures all FLIR cameras via PySpin
- Sets exposure, frame rate, binning, GPIO, chunk data
- Waits for IEEE1588 PTP synchronization (10 seconds)
- Reports serial numbers, resolution, and frame rate per camera

### Phase 3: Endurance Soak (4 hours default)
- Acquires real frames from all cameras at target FPS
- **Noise injection**: replaces pixel data with a cycling pool of 5 random frames per camera before encoding. This defeats temporal compression (each frame differs from the last) while adding zero allocation overhead in the hot path
- Exercises **segment boundaries** every 2 minutes (120 segments in 4 hours, 5,760 in 8 days)
- Full production pipeline: image queues, encoder workers, ffmpeg, metadata journals, SQLite finalize jobs

**Monitoring** (sampled every 60 seconds):
- GPU temperature and thermal throttling detection
- Process RSS with steady-state growth analysis
- Thread count (detects thread leaks)
- File descriptor count (detects descriptor leaks)
- SQLite database size

**Segment cleanup**: a background thread verifies completed segments with ffprobe and deletes them, keeping only the newest 5. This bounds disk usage to ~3-6 GB regardless of test duration.

### Phase 4: Output Verification
Runs ffprobe and metadata validation on remaining segment files.

### Phase 5: Verdict

| Metric | Pass | Warn | Fail |
|--------|------|------|------|
| Dropped frames | 0 | - | >0 |
| Camera timeouts | <1% | 1-5% | >5% |
| Queue utilization | <50% | 50-90% | >=90% |
| GPU temperature | <83C | - | >=83C (throttling) |
| Memory growth | <1 MB/min | 1-10 MB/min | >10 MB/min |
| Thread count growth | 0/hr | - | >0/hr |
| FD count growth | 0/hr | 1-5/hr | >5/hr |
| Segment verification | 100% | - | <100% |

## How It Works

`EnduranceRecorder` subclasses `FlirRecorder` and overrides only the capture loop. Everything else — camera lifecycle, queue/worker management, 4-phase shutdown — is inherited from the production code. This means the test exercises the exact same code paths as a real recording.

```
Real cameras -> PySpin capture -> copy frame -> REPLACE pixels with noise -> enqueue
                                                                              |
                                            production pipeline: encoder -> ffmpeg -> .mp4
                                                                 metadata -> .jsonl -> .json
```

The test runs inside Docker with GPU passthrough, just like production.

## Configuration

All parameters come from the camera config YAML plus CLI overrides:

| Parameter | Source | Default |
|-----------|--------|---------|
| Camera count | `camera-info` keys in YAML | (from config) |
| FPS | `acquisition-settings.frame_rate` | 30 |
| Segment length | `--segment-seconds` | 120s |
| Duration | `-d` / `ENDURANCE_DURATION` | 14400s (4h) |
| Noise injection | `--no-inject-noise` to disable | ON |
| Monitor interval | `--monitor-interval` | 60s |
| Encoder | `--force-cpu` or `nvenc_preset` in YAML | auto-detect |

## Output

```
/data/endurance_test/
  20260331_143000_camera_config/
    report.txt    # human-readable with ANSI colors
    report.json   # machine-readable (for CI/automation)
```

Test recordings are deleted after verification. Only reports are kept.

## Monitoring Overhead

The extended monitor adds three checks per sample on top of the existing GPU temperature probe:

| Check | Cost | Method |
|-------|------|--------|
| Thread count | ~0.1 us | `threading.active_count()` |
| FD count | ~10 us | `os.listdir('/proc/self/fd')` |
| SQLite size | ~1 us | `os.path.getsize()` |
| **Total added** | **~12 us** | |
| Existing GPU temp | ~5 ms | `nvidia-smi` subprocess |

At 60-second intervals, the total monitoring overhead is 0.008% CPU. The recording pipeline will not notice.

## Deployment Workflow

```
1. make build                              # build Docker image
2. make validate                           # hardware + synthetic pipeline (no cameras)
3. make endurance ENDURANCE_DURATION=14400  # 4h real cameras + worst-case encoding
4. QC assistant                            # 20s pre-session quick check

All three pass -> ready for 24/7 deployment.
```
