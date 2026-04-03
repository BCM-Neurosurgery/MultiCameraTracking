# Plan: Post-Incident Fixes for 2026-04-03 OOM Crash

## Context

On 2026-04-03, a Firefox browser left open during a multi-day recording session consumed ~18.7 GB of RAM, exhausting the system's 60 GB + 2 GB swap. This caused the network driver to drop camera packets, killing the recording pipeline. The pipeline hung silently for ~5 hours until manually restarted. See `docs/incident_20260403_oom_crash.md` for full incident report.

This plan addresses the 4 categories of fixes identified: frontend memory leaks, pipeline resilience, infrastructure hardening, and test coverage.

---

## Phase 1: Immediate (before next overnight recording)

### 1A. Docker memory isolation
**File:** `docker-compose.yml`
- Add `mem_limit: 20g` and `memswap_limit: 20g` to the `mocap` service
- This creates a cgroup boundary — host processes (Firefox, Slack) cannot starve the recording container
- The container will OOM-kill internally if it exceeds 20 GB, rather than competing with the host
- 20 GB is generous for 6-camera recording (pipeline uses ~685 MB steady-state); adjust as needed

### 1B. Fix pipeline shutdown under all-cameras-lost
**File:** `multi_camera/acquisition/flir/capture_runner.py`
- The `max_consecutive_timeouts=30` auto-shutdown already exists (line 88-89, 95-96) and raises `RuntimeError`
- Problem: during the incident, streak 30 was logged but the process still hung — the RuntimeError either wasn't propagated cleanly or the shutdown path deadlocked under memory pressure
- Investigate the error propagation path from `capture_runner` through `capture_loop` to `recorder_service.py` to ensure `RuntimeError` leads to graceful shutdown (flush journals, finalize metadata) rather than a hang
- Also check: is the log truncated at streak 30 because the RuntimeError fired but the log handler couldn't flush? Or did the error get swallowed?

### 1C. Fix Video.js Blob URL leak
**File:** `react_frontend/src/components/Video.js`
- Replace stale closure over `imageSrc` with a `useRef` to track previous Blob URL
- Remove `console.log("new image")` on line 24 (logs every frame at 30fps)
- Fix cleanup function to revoke via the ref, not the stale state variable

### 1D. Standardize video encoding format
**File:** `multi_camera/acquisition/flir/workers/encoder_worker.py`
- Add `-pix_fmt yuv420p` (or similar) to `_build_ffmpeg_cmd` output options to convert the GBR 4:4:4 input to standard YUV color space
- This fixes the "green tint" issue seen in QuickTime and file explorer thumbnails, ensuring universal compatibility

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

### 4A. Frontend memory monitoring in stress test
**File:** `multi_camera/acquisition/stress_test/__main__.py` (+ new module)
- Add `--with-frontend` flag or separate `make validate-frontend` target
- Launches FastAPI backend + headless Chromium pointed at `localhost:3000`
- Samples browser process RSS via `psutil` alongside pipeline RSS
- Reports browser memory growth rate; fails if > 50 MB/min
- Requires: `playwright` or `selenium` + headless Chrome in the Docker image

---

## Verification

After each phase:
- **Phase 1:** Run `docker compose config` to verify mem_limit. Start a short recording, verify it works within the memory limit. Open Firefox with Slack, confirm recording is unaffected. Test Video.js fix by opening preview in DevTools Memory tab and confirming Blob count stays flat.
- **Phase 2:** Start a recording, verify heartbeat file is being touched. Kill the container ungracefully, verify the watchdog alerts within 2-4 minutes. Check that logs appear in `VIDEO_DATA_SORTED/` after the sorter runs.
- **Phase 3:** Open Annotator/SmplBrowser, switch recordings multiple times, check DevTools Memory for flat heap. Verify `requestAnimationFrame` stops after `close()`.
- **Phase 4:** Run `make validate-frontend`, verify browser memory is reported in the output.

---

## Files to modify (summary)

| Phase | File | Change |
|-------|------|--------|
| 1A | `docker-compose.yml` | Add `mem_limit`, `memswap_limit` |
| 1B | `multi_camera/acquisition/flir/capture_runner.py` | Investigate/fix RuntimeError propagation |
| 1B | `multi_camera/acquisition/flir/recorder_service.py` | Ensure clean shutdown on RuntimeError |
| 1C | `react_frontend/src/components/Video.js` | Fix Blob URL leak, remove console.log |
| 1D | `multi_camera/acquisition/flir/workers/encoder_worker.py` | Add `-pix_fmt yuv420p` to fix green tint |
| 2A | `scripts/recording_watchdog.sh` (new) | External watchdog script |
| 2A | `multi_camera/acquisition/flir/capture_runner.py` | Add heartbeat touch |
| 2B | `/home/nbusleep/BCM/CODE/data-net-source/parsers/parse_av.py` | Move .log and .metadata.jsonl files |
| 3A | `react_frontend/src/components/visualization_js/viewer.js` | Proper close() disposal |
| 3B | `react_frontend/src/components/visualization_js/selector.js` | Add dispose() method |
| 3C | `multi_camera/acquisition/flir/logging_setup.py` | RotatingFileHandler |
| 4A | `multi_camera/acquisition/stress_test/__main__.py` | Frontend memory monitoring |
