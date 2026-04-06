# Plan: Post-Incident Fixes for 2026-04-03 OOM Crash

## Context

On 2026-04-03, a Firefox browser left open during a multi-day recording session consumed ~18.7 GB of RAM, exhausting the system's 60 GB + 2 GB swap. This caused the network driver to drop camera packets, killing the recording pipeline. The pipeline hung silently for ~5 hours until manually restarted. See `docs/incident_20260403_oom_crash.md` for full incident report.

This plan addresses the 4 categories of fixes identified: frontend memory leaks, pipeline resilience, infrastructure hardening, and test coverage.

---

## Phase 1: Immediate (before next overnight recording)

### 1A. Protect recording from host memory exhaustion

**Problem:** In the incident, Firefox (host process) consumed 18.7 GB, exhausting all 60 GB + 2 GB swap. The kernel couldn't allocate network buffers for the NIC driver, so camera packets were dropped and the pipeline died.

**What does NOT work:** `mem_limit: 20g` on the Docker container. This is a ceiling, not a reservation — it limits how much the container can use, but does not guarantee memory is available. If Firefox eats 55 GB on the host, the container and kernel are both starved regardless of `mem_limit`.

**What does work:**

1. **Limit the browser, not the container.** Launch Firefox with a memory cap so it gets OOM-killed before it starves the system:
   ```bash
   systemd-run --user --scope -p MemoryMax=8G firefox
   ```
   If the browser hits 8 GB, the kernel kills it — but 52 GB remains free, the NIC driver has plenty of memory, and the recording continues uninterrupted. Consider creating a desktop launcher or alias that wraps Firefox with this limit.

2. **Raise `vm.min_free_kbytes`** so the kernel reserves enough memory for itself (including NIC packet buffers), triggering OOM kills earlier:
   ```bash
   # /etc/sysctl.d/99-recording.conf
   vm.min_free_kbytes = 524288   # 512 MB reserved for kernel
   ```
   Default is ~67 MB. The incident failed at `GFP_ATOMIC` allocation when free memory dropped below the 90 MB watermark. Setting this to 512 MB ensures the OOM killer fires well before network allocation fails.

3. **OOM score adjustment** on the recording container so the kernel kills browsers/other processes first:
   ```yaml
   # docker-compose.yml
   services:
     mocap:
       oom_score_adj: -500   # lower = less likely to be killed
   ```

### 1B. Fix pipeline shutdown under all-cameras-lost
**File:** `multi_camera/acquisition/flir/capture_runner.py`
- The `max_consecutive_timeouts=30` auto-shutdown already exists (line 88-89, 95-96) and raises `RuntimeError`
- Problem: during the incident, streak 30 was logged but the process still hung — the RuntimeError either wasn't propagated cleanly or the shutdown path deadlocked under memory pressure
- Investigate the error propagation path from `capture_runner` through `capture_loop` to `recorder_service.py` to ensure `RuntimeError` leads to graceful shutdown (flush journals, finalize metadata) rather than a hang
- Also check: is the log truncated at streak 30 because the RuntimeError fired but the log handler couldn't flush? Or did the error get swallowed?

### 1C. Fix Video.js Blob URL leak — DONE
**File:** `react_frontend/src/components/Video.js`
- Replaced stale closure over `imageSrc` with `useRef` to track previous Blob URL
- Removed `console.log("new image")` (logged every frame at 30fps)
- Fixed cleanup function to revoke via the ref, not the stale state variable
- See `docs/bug_video_js_blob_url_leak.md` for full write-up

### 1D. Standardize video encoding format — DONE
**File:** `multi_camera/acquisition/flir/workers/encoder_worker.py`
- Added `-pix_fmt yuv420p` to `_build_ffmpeg_cmd` output options to convert the GBR 4:4:4 input to standard YUV color space
- Also updated `gpu_detect.py` benchmark command to match
- Fixes the "green tint" issue seen in QuickTime and file explorer thumbnails, ensuring universal compatibility

---

## Phase 2: Short-term (this week)

### 2A. Watchdog + alerting
**New file:** `scripts/recording_watchdog.sh` (or Python equivalent)
- External watchdog that runs via cron every 2 minutes
- Checks: is a recording container running? If yes, has the newest file in the output directory been modified in the last N minutes?
- Alert mechanism: send email or Slack webhook if stale
- This replaces the old file-change monitor (disabled due to false-positive complaints) with a smarter approach: only alert when the pipeline is supposed to be active but isn't producing output
- Also add a heartbeat touch file from the capture loop (e.g., `/tmp/flir_recording_alive`) as a secondary signal

**File:** `multi_camera/acquisition/flir/capture_runner.py`
- In the main frame loop, touch a heartbeat file every N seconds (e.g., 30s)
- Lightweight: just `os.utime(path, None)` or `pathlib.Path.touch()`

### 2B. Log co-location with sorted data
**File:** `/home/nbusleep/BCM/CODE/data-net-source/parsers/parse_av.py`
- The sorter currently moves `.mp4` and `.json` files but ignores `.log` files
- Add `.log` to the file extensions that get moved (or symlinked) to `VIDEO_DATA_SORTED/`
- Also move `.metadata.jsonl` files if they exist (the orphaned `000111.metadata.jsonl` was left behind too)

---

## Phase 3: Medium-term (next 1-2 weeks)

### 3A. Viewer.js proper disposal
**File:** `react_frontend/src/components/visualization_js/viewer.js`
- Rewrite `close()` to properly dispose all Three.js resources:
  - `this.renderer.dispose()`
  - Traverse scene: dispose geometries, materials, textures
  - `this.controls.dispose()`
  - `this.gui.destroy()`
  - Disconnect `ResizeObserver` (store as `this._resizeObserver` in constructor)
  - Remove `window` resize listener (store bound handler as `this._onResize`)
  - Call `this.selector.dispose()`
- Add `this._closed = true` flag; guard `animate()` with early return if closed
- Fix `window.onload` assignment (line 256) — use `addEventListener` instead

### 3B. Selector.js dispose method
**File:** `react_frontend/src/components/visualization_js/selector.js`
- Store bound handlers as instance properties instead of inline `.bind(this)`
- Add `dispose()` method that calls `removeEventListener` for all three pointer events

### 3C. Log rotation safety net
**File:** `multi_camera/acquisition/flir/logging_setup.py`
- Replace `FileHandler` with `RotatingFileHandler` (e.g., 50 MB max, keep 5 backups)
- This prevents unbounded log growth during multi-day sessions

---

## Phase 4: Longer-term

### 4A. Frontend memory monitoring in stress test — DONE
**File:** `multi_camera/acquisition/stress_test/_frontend.py` (new), `__main__.py`, `_runner.py`
- `make validate` now includes `--with-frontend` by default
- Launches headless Chromium via Playwright against the React app
- Tracks blob URL leaks via JS injection (`createObjectURL`/`revokeObjectURL` counting with byte accounting)
- Tracks JS heap growth and DOM node count via CDP `Performance.getMetrics`
- Sends preview frames at realistic rate (~3fps, matching `capture_runner.py` every-10th-frame logic)
- Playwright + Chromium baked into Docker image
- Test confirmed the blob leak: 353 URLs leaked in 2 min, 0 revoked, 139 MB/min growth

---

## Verification

After each phase:
- **Phase 1:** Run `make validate` — Blob URLs row should show "all revoked". Set up `systemd-run` wrapper for Firefox, verify it gets killed at 8 GB without affecting a test recording. Verify `vm.min_free_kbytes` is set via `sysctl vm.min_free_kbytes`.
- **Phase 2:** Start a recording, verify heartbeat file is being touched. Kill the container ungracefully, verify the watchdog alerts within 2-4 minutes. Check that logs appear in `VIDEO_DATA_SORTED/` after the sorter runs.
- **Phase 3:** Open Annotator/SmplBrowser, switch recordings multiple times, check DevTools Memory for flat heap. Verify `requestAnimationFrame` stops after `close()`.
- **Phase 4:** ~~Run `make validate-frontend`, verify browser memory is reported in the output.~~ Done — `make validate` now includes frontend checks automatically.

---

## Files to modify (summary)

| Phase | File | Change |
|-------|------|--------|
| 1A | `/etc/sysctl.d/99-recording.conf` | Set `vm.min_free_kbytes=524288` |
| 1A | `docker-compose.yml` | Add `oom_score_adj: -500` to mocap service |
| 1A | Desktop launcher / alias | Wrap Firefox with `systemd-run --user --scope -p MemoryMax=8G` |
| 1B | `multi_camera/acquisition/flir/capture_runner.py` | Investigate/fix RuntimeError propagation |
| 1B | `multi_camera/acquisition/flir/recorder_service.py` | Ensure clean shutdown on RuntimeError |
| 1C | `react_frontend/src/components/Video.js` | ~~Fix Blob URL leak, remove console.log~~ DONE |
| 1D | `multi_camera/acquisition/flir/workers/encoder_worker.py` | ~~Add `-pix_fmt yuv420p` to fix green tint~~ DONE |
| 2A | `scripts/recording_watchdog.sh` (new) | External watchdog script |
| 2A | `multi_camera/acquisition/flir/capture_runner.py` | Add heartbeat touch |
| 2B | `/home/nbusleep/BCM/CODE/data-net-source/parsers/parse_av.py` | Move .log and .metadata.jsonl files |
| 3A | `react_frontend/src/components/visualization_js/viewer.js` | Proper close() disposal |
| 3B | `react_frontend/src/components/visualization_js/selector.js` | Add dispose() method |
| 3C | `multi_camera/acquisition/flir/logging_setup.py` | RotatingFileHandler |
| 4A | `multi_camera/acquisition/stress_test/_frontend.py` | ~~Frontend memory monitoring~~ DONE |
