# Bug: `queue.join()` deadlock when worker exits on error without draining

## Problem

When a worker thread (`write_journal_queue`, `write_metadata_queue`) encounters an error, it `break`s out of its loop after calling `task_done()` for the current item only. Any items already sitting in the queue (or later enqueued by the capture loop) remain unprocessed with their `unfinished_tasks` counter still incremented.

During shutdown, `recorder_service.stop_workers()` calls `image_queue.join()` / `json_queue.join()`, which block until **every** item's `task_done()` has been called. Since the dead worker will never drain the remaining items, `join()` blocks forever and the process hangs.

## Affected files

- `flir/workers/journal_writer_worker.py` — `write_journal_queue()`
- `flir/workers/metadata_workers.py` — `write_metadata_queue()`
- `flir/recorder_service.py` — `stop_workers()`

## How it happens

### 1. Worker hits an error and exits early

```python
# journal_writer_worker.py, write_journal_queue()

while True:
    frame = image_queue.get()          # blocks until item available
    try:
        if frame is None:
            break
        ...
        journal.write_frame(im, ...)   # <-- raises (e.g. disk full)
    except Exception as exc:
        worker_error_state["message"] = str(exc)
        worker_error_state["event"].set()
        break                          # <-- exits loop, queue NOT drained
    finally:
        image_queue.task_done()        # only covers THIS item
# remaining items in image_queue: unfinished_tasks > 0
```

### 2. Capture loop keeps enqueuing frames after worker death

The capture loop in `capture_runner.py` does not check `writer_error` between frames (it checks `stop_recording` only). It continues to `safe_put()` frames onto the image queue. These items increment the queue's internal `unfinished_tasks` counter but will never be consumed.

### 3. Shutdown calls `queue.join()` — deadlock

```python
# recorder_service.py, stop_workers()

for serial, image_queue in self.recorder.image_queue_dict.items():
    image_queue.put(None)       # sentinel enqueued but worker is dead
    image_queue.join()          # BLOCKS FOREVER — unfinished_tasks > 0
    #                             ^^^^^^^^^^^^^^^
    #                             worker is dead, nobody will call task_done()
```

The same pattern exists for `json_queue`:

```python
self.recorder.json_queue.put(None)
self.recorder.json_queue.join()     # also deadlocks if metadata writer died
```

## Reproduction scenario

1. Start 8-camera continuous recording
2. Fill the disk (or simulate with `chmod 000` on the output directory)
3. `write_journal_queue` for one camera raises `OSError`, sets `writer_error`, breaks
4. Capture loop continues enqueuing frames for ~seconds before noticing the error
5. `stop_workers()` is called — hangs on `image_queue.join()` for the dead camera
6. Process is unkillable without `kill -9` (worker threads are non-daemon)

## Suggested fix

Replace `queue.join()` with thread-join-based shutdown. The sentinel `None` is still enqueued, but we wait on the **thread** (which has a timeout) rather than the **queue** (which has no timeout):

```python
# recorder_service.py, stop_workers()

# --- image workers ---
for serial, image_queue in self.recorder.image_queue_dict.items():
    image_queue.put(None)

for thread in handles.image_threads:
    thread.join(timeout=10)
    if thread.is_alive():
        tqdm.write(f"WARNING: {thread.name} did not exit within timeout")

# --- metadata writer ---
if not self.recorder.writer_error["event"].is_set():
    self.recorder.json_queue.put(None)
elif handles.metadata_writer_thread is not None and handles.metadata_writer_thread.is_alive():
    try:
        self.recorder.json_queue.put(None, timeout=1.0)
    except queue.Full:
        pass

if handles.metadata_writer_thread is not None:
    handles.metadata_writer_thread.join(timeout=10)
```

Additionally, the worker error path should drain its queue before exiting to prevent stale `unfinished_tasks`:

```python
# journal_writer_worker.py, write_journal_queue() — error path

except Exception as exc:
    worker_error_state["message"] = str(exc)
    worker_error_state["event"].set()
    # drain remaining items so queue.join() doesn't block
    while True:
        try:
            leftover = image_queue.get_nowait()
        except queue.Empty:
            break
        image_queue.task_done()
    break
```

## Related issues

- Non-daemon worker threads prevent `kill` from terminating the process (compounds this deadlock)
- `queue.get()` with no timeout means even a *healthy* worker hangs forever if the capture loop dies without sending a sentinel
