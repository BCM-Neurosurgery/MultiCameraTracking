# Bug: Race condition loses last segment's encode/finalize job on shutdown

## Problem

The encode worker and metadata finalizer both use the same exit condition:

```python
if stop_event.is_set() and repo.count_pending(conn) == 0:
    break
```

This races with the journal/metadata writers that flush their final segment during shutdown. The worker can observe `count_pending == 0`, exit, and then the final job is enqueued — with nobody left to process it.

## Affected files

- `flir/workers/encode_worker.py` — `encode_jobs_worker()` (lines 126-128)
- `flir/workers/metadata_workers.py` — `metadata_finalize_queue()` (lines 98-99)

## The race

### Timeline for encode worker

```
Thread: journal_writer        Thread: encode_worker         Thread: stop_workers
─────────────────────────    ─────────────────────────     ─────────────────────────
                              polling for jobs...
                                                            encode_stop_event.set()
                              stop_event.is_set() → True
                              count_pending() → 0  ✓
                              break  ← EXITS
receives sentinel None
finally: _flush_journal_to_encode_job()
  journal.close()
  repo.enqueue_job(...)       ← job enqueued, but worker already exited
                              (worker is dead)
```

The journal writer's `finally` block at `journal_writer_worker.py:127-128` flushes the last segment **after** consuming the sentinel `None`. But by this point, `stop_workers()` has already set `encode_stop_event`, and the encode worker may have already checked `count_pending == 0` and exited.

### Same race for metadata finalizer

```
Thread: metadata_writer       Thread: metadata_finalizer    Thread: stop_workers
─────────────────────────    ─────────────────────────     ─────────────────────────
                              polling for jobs...
                                                            finalize_stop_event.set()
                              stop_event.is_set() → True
                              count_pending() → 0  ✓
                              break  ← EXITS
receives sentinel None
finally: repo.enqueue_job()   ← job enqueued, but finalizer already exited
                              (finalizer is dead)
```

### Why the current ordering in `stop_workers` causes this

```python
# recorder_service.py, stop_workers()

# 1. Send sentinels to journal writers and wait for them to finish
for serial, image_queue in self.recorder.image_queue_dict.items():
    image_queue.put(None)
for thread in handles.image_threads:
    thread.join(timeout=10)

# 2. Send sentinel to metadata writer and wait
self.recorder.json_queue.put(None)
handles.metadata_writer_thread.join(timeout=10)

# 3. Signal finalize worker to stop  ← finalizer may exit before step 2's
self.recorder.finalize_stop_event.set()  # finally block enqueues the last job
handles.metadata_finalize_thread.join(timeout=20)

# 4. Signal encode worker to stop  ← encode worker may exit before step 1's
self.recorder.encode_stop_event.set()    # finally block enqueues the last job
for thread in handles.encode_threads:
    thread.join(timeout=30)
```

The problem is that `thread.join(timeout=10)` returns when the journal writer **thread exits**, but the journal writer's `finally` block (which enqueues the encode job) runs **before** the thread exits. So by the time `encode_stop_event.set()` is called at step 4, the final job should already be enqueued.

**Wait — is there actually a race?** Let me re-examine the ordering:

1. `image_queue.put(None)` → journal writer receives it, `break`s, `finally` runs `_flush_journal_to_encode_job` → enqueues last encode job → thread exits
2. `thread.join(timeout=10)` returns → the encode job is guaranteed to be in SQLite
3. `encode_stop_event.set()` → encode worker sees it, checks `count_pending` → should see the job

**The race exists when `thread.join` times out.** If the journal writer takes >10s to flush (e.g. slow fsync, SQLite contention), `thread.join(timeout=10)` returns with the thread still alive. `stop_workers` proceeds to set `encode_stop_event`. The encode worker checks `count_pending == 0` (job not yet enqueued), exits. Then the journal writer finally enqueues the job — too late.

The same race applies to the metadata path: if `metadata_writer_thread.join(timeout=10)` times out, `finalize_stop_event.set()` fires before the final finalize job is enqueued.

## Impact

- Last segment of every recording session has no MP4 and/or no `.json` metadata
- The orphaned encode/finalize job sits in SQLite as `pending` — recovered on next startup by `reset_in_progress_jobs`, but only if the next session uses the same DB path
- For one-off recordings (not 24/7), the last segment is permanently lost

## Suggested fix

Set the stop events **before** sending sentinels, and change the poll-based workers to do a final drain after the stop event:

```python
# encode_worker.py — drain remaining jobs after stop_event

while True:
    if stop_event.is_set() and repo.count_pending(conn) == 0:
        # Final check: sleep briefly and re-check to close the race window.
        time.sleep(0.5)
        if repo.count_pending(conn) == 0:
            break

    job = repo.claim_next_job(conn, worker_id=worker_id)
    if job is None:
        time.sleep(0.3)
        continue
    ...
```

The double-check with a sleep window gives the journal writer time to enqueue its final job after `stop_event` is set. The same pattern applies to `metadata_finalize_queue`.

This is a simple, conservative fix. The 0.5s delay only fires once at shutdown — it doesn't affect steady-state performance.

## Related issues

- Thread join timeouts (issue #1, fixed) — the timeout is what opens the race window
- `queue.get()` no timeout (issue #2, fixed) — ensures workers can observe stop_event
