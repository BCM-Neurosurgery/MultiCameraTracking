# Bug: Thread join timeouts silently ignored — zombie threads accumulate

## Problem

`stop_workers()` uses `thread.join(timeout=N)` throughout. When the timeout expires and the thread is still alive, the code logs a warning (for some threads) and moves on. But the thread keeps running — holding file handles, DB connections, queue references, and CPU time. Over repeated start/stop cycles in 24/7 operation, these zombie threads accumulate.

## Affected file

- `flir/recorder_service.py` — `stop_workers()` (lines 141-173)

## Current code

```python
# recorder_service.py, stop_workers()

# Image writer threads — warns but continues
for thread in handles.image_threads:
    thread.join(timeout=5)
    if thread.is_alive():
        tqdm.write(f"WARNING: {thread.name} did not exit within timeout")
        # thread keeps running... holding file handle, encode_jobs_db connection

# Encode worker threads — no warning at all
for thread in handles.encode_threads:
    thread.join(timeout=30)
    # if still alive: ffmpeg subprocess + DB connection leak

# Metadata writer thread — warns but continues
if handles.metadata_writer_thread is not None:
    handles.metadata_writer_thread.join(timeout=5)
    if handles.metadata_writer_thread.is_alive():
        tqdm.write(f"WARNING: ...")
        # thread keeps running... holding .metadata.jsonl file handle

# Metadata finalize thread — no warning at all
if handles.metadata_finalize_thread is not None:
    handles.metadata_finalize_thread.join(timeout=20)
    # if still alive: DB connection + records_queue reference leak
```

## What each zombie holds

| Thread | Resources held | Impact |
|--------|---------------|--------|
| Journal writer | Open `.journal` file handle, `EncodeJobsRepo` ref | File descriptor leak, stale lock on journal file |
| Encode worker | SQLite connection, ffmpeg subprocess, tmp `.mp4` file | DB lock contention, orphan ffmpeg process, disk space |
| Metadata writer | Open `.metadata.jsonl` file handle, `FinalizeJobsRepo` ref | File descriptor leak, stale journal |
| Metadata finalizer | SQLite connection, `records_queue` ref | DB lock contention, queue ref prevents GC |

## How it accumulates

```
Session 1: start_workers() → 8 image + 1 encode + 1 metadata + 1 finalize = 11 threads
            stop_workers() → encode worker times out (slow ffmpeg) → 1 zombie
Session 2: start_workers() → 11 new threads + 1 zombie = 12 threads
            stop_workers() → encode worker times out again → 2 zombies
...
Session 50: 11 new threads + 49 zombies = 60 threads
            49 orphan ffmpeg processes, 49 SQLite connections
```

Each zombie encode worker may also hold a running `ffmpeg` subprocess. On an 8-camera system that's 8 potential ffmpeg processes per zombie cycle.

## Suggested fix

Non-daemon threads that fail to exit within the timeout need to be interrupted, not ignored. Since Python threads can't be killed, the fix is to ensure threads **can always observe the stop signal** and exit. Our previous fixes (#2: `get(timeout=1.0)` + `stop_event`) already ensure this for journal and metadata writers.

For encode workers, the concern is a long-running `ffmpeg` encode. The fix is:

1. **Check `stop_event` between jobs** (already done)
2. **Add a hard timeout per encode job** — kill the ffmpeg subprocess if it exceeds a limit
3. **Log and escalate** if a thread is still alive after join timeout

```python
# recorder_service.py, stop_workers() — escalation pattern

for thread in handles.image_threads:
    thread.join(timeout=5)
    if thread.is_alive():
        tqdm.write(f"ERROR: {thread.name} still alive after timeout — "
                   f"zombie thread, resources may leak")

for thread in handles.encode_threads:
    thread.join(timeout=60)
    if thread.is_alive():
        tqdm.write(f"ERROR: {thread.name} still alive after timeout — "
                   f"zombie thread, ffmpeg subprocess may be orphaned")

if handles.metadata_finalize_thread is not None:
    handles.metadata_finalize_thread.join(timeout=20)
    if handles.metadata_finalize_thread.is_alive():
        tqdm.write(f"ERROR: {handles.metadata_finalize_thread.name} still alive after timeout")
```

This doesn't prevent the zombie, but it makes it visible. The real prevention is the combination of:
- `stop_event` + `get(timeout)` (issues #1/#2, fixed) — writers can always exit
- `flush_done_event` (issue #4, fixed) — shutdown waits for flush before signaling drain
- Generous but finite timeouts — give threads enough time to finish naturally

The remaining edge case is an encode worker stuck on a very large ffmpeg job. For that, the encode worker should check `stop_event` periodically and terminate the subprocess if it's set:

```python
# encode_worker.py, _encode_journal_to_mp4() — interruptible ffmpeg

proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, ...)
decoded_count = 0
try:
    for jpeg_data in _iter_journal_frames(job.journal_path):
        if stop_event.is_set():
            proc.kill()
            raise RuntimeError("encode interrupted by shutdown")
        bayer = cv2.imdecode(...)
        rgb = cv2.cvtColor(bayer, cvt_code)
        proc.stdin.write(rgb.tobytes())
        decoded_count += 1
    ...
```

This makes the encode worker interruptible at frame granularity (~33ms at 30fps). The partially-encoded tmp file is cleaned up by the existing failure handler.

## Related issues

- `queue.get()` no timeout (issue #2, fixed) — ensures writers can exit
- `flush_done_event` (issue #4, fixed) — prevents premature stop_event
- Non-daemon threads (issue #7) — zombie threads prevent process exit; daemon=True is not the fix because it causes data corruption (see issue #2 doc)
