# Pipeline Reliability Audit — 2026-03-29

Comprehensive audit of the FLIR acquisition pipeline for 24/7 clinical recording.
Covers crash durability, thread safety, error propagation, and failure modes.

---

## P0 — CRITICAL (will cause data loss or system hang)

### 1. `_close_ffmpeg()` blocks forever on disk issues
**File:** `multi_camera/acquisition/flir/workers/encoder_worker.py:72-93`

`proc.wait()` has no timeout. If ffmpeg hangs (disk full, NFS stall, slow storage),
the encoder thread blocks forever, image queue fills up, capture loop blocks, and the
entire system freezes. No watchdog, no recovery path.

**Trigger:** Disk fills to 100%, NFS mount becomes unresponsive, or slow NVMe during
sustained write.

**Fix:** Add `proc.wait(timeout=5.0)` with `proc.kill()` fallback.

---

### 2. Metadata not durably written — crash loses frames
**File:** `multi_camera/acquisition/flir/workers/metadata_workers.py`

`.metadata.jsonl` uses line buffering (`buffering=1`) but no `fsync()`. Power outage
loses last N frames of metadata sitting in OS page cache.

Worse: a truncated final JSON line causes `finalize_legacy_json()` to throw
`JSONDecodeError` (no try/except around `json.loads(line)`). The job fails 3 times
and is permanently abandoned — no `.json` file is ever produced for that segment.

**Trigger:** Power outage or kill -9 during active recording.

**Fix:**
- Add `os.fsync()` on segment boundary close.
- Wrap `json.loads()` in try/except in `finalize_legacy_json()` to skip corrupt lines
  instead of failing the entire segment.

---

### 3. Single camera failure kills all cameras
**File:** `multi_camera/acquisition/flir/capture_runner.py:88-96`

If one camera hits 30 consecutive timeouts (loose cable, firmware hang, PoE glitch),
a `RuntimeError` stops the entire recording across all 6 cameras. In a clinical
session, one flaky cable means total data loss, not degraded recording.

**Trigger:** One camera's Ethernet cable is bumped, one PoE port flickers, or one
camera firmware becomes unresponsive.

**Fix:** Mark failed camera as inactive and continue recording with remaining cameras.
Make behavior configurable (abort vs. degrade).

---

### 4. Concurrent `/new_trial` calls start dual recordings
**File:** `multi_camera/backend/fastapi.py:356-415`

No mutex at the API layer. Two quick clicks on "New Trial" (or automated retry) both
pass the `_acquiring` check before either thread sets the flag. Two threads then fight
over the same cameras, queues, and ffmpeg processes.

**Trigger:** Double-click on "New Trial" button, or network retry sends duplicate POST.

**Fix:** Add `asyncio.Lock()` around the `/new_trial` and `/stop` endpoints.

---

### 5. Zombie threads on partial worker startup failure
**File:** `multi_camera/acquisition/flir_recording_api.py:335`

If `start_workers()` partially succeeds (some threads started) then throws an
exception, `worker_handles` is None, so the `finally` block skips `stop_workers()`.
Running threads become zombies consuming resources. Next `start_acquisition()` call
may fail or behave unpredictably.

**Trigger:** I/O error during SQLite job DB init, or thread creation failure under
resource pressure.

**Fix:** `start_workers()` should track partially-started threads internally and
clean them up on exception before re-raising.

---

### 6. No IEEE1588 sync verification
**File:** `multi_camera/acquisition/flir_recording_api.py:101-111`

`synchronize_cameras()` enables PTP and sleeps 10 seconds but never checks if sync
actually succeeded (never reads `GevIEEE1588Status`). If PTP master is lost
mid-recording, cameras silently free-run. Frames from different cameras drift apart
and multi-camera pose estimation produces garbage with no warning.

**Trigger:** Network congestion prevents PTP sync, or PTP master camera reboots
mid-session.

**Fix:** Verify `GevIEEE1588Status` is "Master" or "Slave" after sync wait. Monitor
`GevIEEE1588OffsetFromMaster` periodically during capture loop and warn if drift
exceeds threshold.

---

## P1 — HIGH (significant reliability gaps for 24/7)

### 7. Metadata finalize sentinel silently dropped
**File:** `multi_camera/acquisition/flir/recorder_service.py:196`

On error shutdown, if metadata queue is full, `queue.put(None, timeout=1.0)` raises
`Full` which is caught with bare `pass`. Metadata writer never gets the stop signal,
keeps looping on `queue.get(timeout=1.0)`, never flushes files. Thread becomes zombie
and last segment's metadata may not be written.

**Trigger:** Error during recording + metadata queue is backed up (slow disk).

**Fix:** Never silently ignore `queue.Full` for sentinel puts. Force-drain the queue
if needed, or use a separate stop event.

---

### 8. No timeout on Spinnaker SDK calls
**File:** `multi_camera/acquisition/flir/camera_control.py`

Camera property writes (`c.AcquisitionFrameRate = ...`, `c.GevSCPSPacketSize = ...`)
can block indefinitely if camera is unresponsive. One hung camera blocks the entire
`ThreadPoolExecutor` in `configure_cameras()`. The API endpoint hangs with no error
returned to the user.

**Trigger:** Camera firmware hang, network cable unplugged during initialization.

**Fix:** Wrap SDK calls with thread-based timeout, or set socket-level timeouts on
GigE connections.

---

### 9. WebSocket manager has no thread safety
**File:** `multi_camera/backend/fastapi.py:95-113`

`active_connections` list is accessed from multiple async tasks with no lock.
Concurrent connect/disconnect/broadcast can corrupt the list or raise
`ValueError`/`RuntimeError`. A stale WebSocket from a disconnected client stays in
the list and can delay graceful shutdown by 30-60 seconds.

**Trigger:** Multiple browser tabs open, one tab closed while broadcast is in
progress.

**Fix:** Add `threading.Lock()` around list mutations. Remove stale connections on
send failure during broadcast.

---

### 10. Finalize jobs permanently abandoned after 3 retries
**File:** `multi_camera/acquisition/flir/storage/finalize_jobs_repo.py`

After 3 failures (e.g., from a corrupted `.jsonl` that will never parse), job is
marked `failed` forever. No `.json` output for that segment. No alerting. Backend
never learns the segment exists. Failed jobs accumulate in SQLite unbounded.

**Trigger:** Power outage corrupts `.metadata.jsonl` → finalization fails 3x →
segment permanently orphaned.

**Fix:** Add graceful corruption handling (skip bad lines in `.jsonl`). Add periodic
cleanup of old failed jobs. Consider alerting on permanent failures.

---

## P2 — MEDIUM (edge cases that compound over 24/7)

### 11. `set_worker_error()` race condition
**File:** `multi_camera/acquisition/flir/pipeline/queues.py:36-43`

Two workers calling `set_worker_error()` simultaneously can interleave writes: event
gets `.set()` before message is written. Main thread reads `event.is_set()` but gets
`None` for message.

**Fix:** Use a `threading.Lock()` or write message before setting event.

---

### 12. Timeout streaks not cleared on segment rollover
**File:** `multi_camera/acquisition/flir/capture_runner.py:183`

Accumulated timeout count carries across segment boundaries. 25 timeouts at end of
segment 1 + 1 timeout at start of segment 2 = triggers abort even though the new
segment just started.

**Fix:** Reset `timeout_streaks` dict on segment rollover.

---

### 13. Recording failure returns HTTP 200
**File:** `multi_camera/backend/fastapi.py:387-413`

`task_done_callback` catches recording exceptions and logs them, but the original
`/new_trial` endpoint already returned HTTP 200. Client thinks recording succeeded.

**Fix:** Store error state and expose via `/recording_status` endpoint or WebSocket
push.

---

### 14. `collect_records()` races with finalize thread
**File:** `multi_camera/acquisition/flir/recorder_service.py:215-221`

Records are collected with `records_queue.empty()` while the finalize thread may
still be enqueuing. Gets partial results.

**Fix:** Use a proper shutdown barrier — wait for finalize thread to exit before
collecting records.

---

### 15. GlobalState accessed without synchronization
**File:** `multi_camera/backend/fastapi.py:70-84`

`recording_status` and `current_session` written from worker threads and read from
API routes with no lock. Can produce inconsistent reads.

**Fix:** Add `threading.RLock()` to GlobalState.

---

### 16. Camera resource leak on partial initialization
**File:** `multi_camera/acquisition/flir_recording_api.py:208`

If camera 3 of 6 fails in `configure_cameras()`, cameras 0-2 are left initialized
with open Spinnaker handles. Next retry creates new handles without closing old ones.

**Fix:** On any initialization failure, close all successfully-initialized cameras
before re-raising.

---

## Crash Durability Summary

| Component | Kill -9 | Power loss |
|-----------|---------|------------|
| Video (MP4) | Last keyframe survives (fragmented MP4) | Last 1-2 frames lost (no fsync) |
| Metadata (.jsonl) | Last few lines lost | Last few lines lost + possible corruption |
| Metadata (.json) | Not built if finalize crashes | Same + cascading failure |
| Job tracking (SQLite) | Safe (WAL mode) | Safe (WAL mode) |
| In-flight frames (queues) | Lost (RAM only) | Lost (RAM only) |

Video data is reasonably durable thanks to ffmpeg fragmented MP4. Metadata is the
weak link — one crash can cascade into permanent segment loss via corrupted `.jsonl`
failing finalization.
