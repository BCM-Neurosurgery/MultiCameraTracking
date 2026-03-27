# Deployment Validation

Validates that a system can handle 24/7 multi-camera recording under worst-case load. Run this after deploying to a new site before starting real recordings.

## Quick Start

```bash
make validate              # 5-min soak (reads camera_config.yaml from /configs)
make validate DURATION=600 # 10-minute thermal soak
```

## What It Tests

### Phase 1: Preflight (~15 seconds)
- **GPU** — detected, NVENC functional
- **NVENC sessions** — N concurrent encodes (one per camera) all succeed
- **RAM** — enough for queue buffers (~2.3 MB × queue_size × cameras)
- **File descriptors** — ulimit high enough for ffmpeg + file handles
- **Volume type** — warns if data volume is NFS/CIFS (SQLite WAL unreliable)
- **Disk write speed** — sequential 256 MB write with fsync
- **Disk metadata latency** — p99 of 500 file create/sync ops (simulates segment rollovers)

### Phase 2: Soak Test (5 minutes default)
- Generates **random noise frames** (worst-case for video encoding — maximum entropy, no compression shortcuts)
- Feeds them through the **real pipeline**: queues → encoder workers → ffmpeg → MP4 + metadata
- Segment rollover every ~33 seconds (1000 frames) to stress the boundary path
- **Monitors** GPU temperature and process memory every 2 seconds
- **Tracks** drops, queue depth, and queue spikes at segment boundaries

### Phase 3: Output Verification (~10 seconds)
- **ffprobe** every MP4 file to confirm playability
- **Parse** every `.metadata.jsonl` line as valid JSON with expected fields
- **Parse** every aggregate `.json` file
- **Check** segment count matches expected

### Verdict
- **PASS** — zero drops, all outputs valid, GPU stable, no memory leaks
- **WARN** — non-fatal issues (low disk, high metadata latency, etc.)
- **FAIL** — drops, corrupt output, NVENC session limit exceeded, thermal throttling

## How It Works

The test runs **inside the Docker container** (same GPU passthrough, same filesystem mounts, same Python environment as real recording). It replaces only the camera layer with synthetic frame generation — everything downstream is the real pipeline.

Test recordings are written to `/data/stress_test/` (the real data volume) to benchmark actual disk I/O, then **deleted after verification**. Only `report.txt` and `report.json` are kept.

## Configuration

All recording parameters come from the camera config YAML (same file used for real recording):

| Parameter | Source | Example |
|-----------|--------|---------|
| Camera count | `camera-info` keys | 3 |
| FPS | `acquisition-settings.frame_rate` | 30 |
| Segment size | `acquisition-settings.video_segment_len` | 18000 |
| Queue size | `acquisition-settings.image_queue_size` | 150 |
| NVENC preset | `acquisition-settings.nvenc_preset` | auto |

The test caps segment size at 1000 frames (~33s at 30fps) so that segment boundaries are exercised multiple times within the soak window, regardless of the real config's segment size.

## Output

```
/data/stress_test/
  20260327_143000_camera_config/
    report.txt    # human-readable
    report.json   # machine-readable (for CI/automation)
```

## What It Cannot Test

These require physical cameras connected:
- GigE network throughput and packet loss
- IEEE1588 PTP synchronization
- Camera hardware reliability
- Arduino trigger / GPIO behavior
