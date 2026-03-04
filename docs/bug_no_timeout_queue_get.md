# Bug: No timeout on `queue.get()` — unrecoverable hang if producer dies

## Problem

All three queue-consumer workers use `queue.get()` with no timeout. If the producer (capture loop) dies without sending a sentinel `None` — e.g. PySpin segfault, unhandled exception, `kill -9` on the main thread — the worker threads block on `get()` forever. Combined with non-daemon threads, the process cannot exit.

## Affected files

| Worker | File | Line | Queue |
|--------|------|------|-------|
| Journal writer | `journal_writer_worker.py` | 97 | `image_queue` |
| Metadata writer | `metadata_workers.py` | 142 | `json_queue` |
| Metadata finalizer | `metadata_workers.py` | 97-103 | SQLite poll (different pattern, same family) |
| Encode worker | `encode_worker.py` | 126-132 | SQLite poll (same) |

The journal and metadata writers are the critical ones — they use a bare blocking `get()`.

## Current code

### Journal writer — blocks forever on dead capture loop

```python
# journal_writer_worker.py:96-97

while True:
    frame = image_queue.get()   # no timeout — blocks until item or sentinel
    try:
        if frame is None:       # sentinel for graceful exit
            break
        ...
```

### Metadata writer — same pattern

```python
# metadata_workers.py:141-142

while True:
    frame = json_queue.get()    # no timeout — blocks forever
    try:
        if frame is None:
            break
        ...
```

### Encode worker and metadata finalizer — poll loop with sleep

```python
# encode_worker.py:126-132

while True:
    if stop_event.is_set() and repo.count_pending(conn) == 0:
        break                   # only exits if stop_event is set
    job = repo.claim_next_job(conn, worker_id=worker_id)
    if job is None:
        time.sleep(0.3)         # poll-based, will exit when stop_event set
        continue
```

The encode/finalize workers are less affected because they check `stop_event` each iteration. But the journal and metadata writers have **no way to observe shutdown** except through the sentinel `None` on their queue.

## How it happens

### Scenario: PySpin segfault during capture

1. Capture loop calls `get_image_with_timeout()` — PySpin segfaults
2. The C-level crash kills only the thread running the capture loop (or the entire interpreter in the worst case)
3. If the main thread survives and enters `stop_workers()`:
   - It puts `None` on each image queue — journal writers receive it and exit normally
   - It puts `None` on `json_queue` — metadata writer receives it and exits normally
   - **This path works** (barely)
4. If the main thread does NOT survive (e.g. `os._exit`, `SIGKILL`, or the segfault takes down the process):
   - Worker threads are blocked in `queue.get()` with no timeout
   - Non-daemon threads prevent process exit
   - Process hangs as a zombie until `kill -9`

### Scenario: Unhandled exception in capture loop before sentinels

1. `capture_runner.run_capture_loop()` raises an unexpected exception (e.g. `KeyError` from malformed `gpio_settings`)
2. Exception propagates to `flir_recording_api.py:303`, caught by `finally` at line 306
3. `stop_workers()` is called — it puts sentinels, this works
4. **But**: if `stop_workers()` itself throws before sending all sentinels (e.g. first `image_queue.put(None)` succeeds but a later one hangs because the queue is full and the worker is dead), remaining workers block forever

### Scenario: Watchdog or supervisor sends SIGTERM

1. Process supervisor decides to restart after detecting a hang
2. Sends `SIGTERM` — Python raises `SystemExit` in the main thread
3. Worker threads are non-daemon, so Python waits for them to finish
4. Journal/metadata workers are blocked in `queue.get()` — they never finish
5. Supervisor escalates to `SIGKILL` after timeout

## Impact

- Process becomes unkillable via normal signals in all failure scenarios
- Systemd/supervisor must use `SIGKILL`, losing any in-flight work
- On bare-metal 24/7 systems without a supervisor, requires manual `kill -9`
- Compounds with issue #1 (queue.join deadlock) — both shutdown paths are blocked

## Suggested fix

Replace bare `queue.get()` with a timeout loop that checks a stop event:

### Journal writer

```python
# journal_writer_worker.py

def write_journal_queue(
    image_queue: Queue,
    serial: str,
    pixel_format: str,
    acquisition_fps: float,
    encode_jobs_db: str,
    worker_error_state: dict,
    stop_event: threading.Event,       # <-- new parameter
):
    current_base = None
    journal = None
    repo = EncodeJobsRepo(encode_jobs_db)

    try:
        while True:
            try:
                frame = image_queue.get(timeout=1.0)
            except queue.Empty:
                if stop_event.is_set():
                    break
                continue

            try:
                if frame is None:
                    break
                ...
            except Exception as exc:
                ...
            finally:
                image_queue.task_done()
    finally:
        _flush_journal_to_encode_job(repo, journal, acquisition_fps)
```

### Metadata writer

```python
# metadata_workers.py

def write_metadata_queue(
    json_queue: Queue,
    ...
    stop_event: threading.Event,       # <-- new parameter
):
    ...
    try:
        while True:
            try:
                frame = json_queue.get(timeout=1.0)
            except queue.Empty:
                if stop_event.is_set():
                    break
                continue
            ...
    finally:
        ...
```

### Recorder service — pass stop event to workers

```python
# recorder_service.py, start_workers()

# For journal writers:
thread = threading.Thread(
    target=write_journal_queue,
    kwargs={
        ...
        "stop_event": self.recorder.encode_stop_event,
    },
)

# For metadata writer:
thread = threading.Thread(
    target=write_metadata_queue,
    kwargs={
        ...
        "stop_event": self.recorder.finalize_stop_event,
    },
)
```

The sentinel `None` remains the normal exit path. The `stop_event + timeout` is the fallback that prevents indefinite blocking when the sentinel never arrives.

## Related issues

- Non-daemon worker threads prevent `SIGTERM` from terminating the process (compounds this)
- `queue.join()` deadlock (issue #1) — already fixed, but this issue makes the thread.join() timeout in that fix actually fire
