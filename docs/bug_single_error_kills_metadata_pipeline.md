# Bug: Single metadata write/finalize failure kills entire metadata pipeline

## Problem

Both metadata workers (`write_metadata_queue` and `metadata_finalize_queue`) use `break` on the first exception, halting the entire worker for the rest of the session. In contrast, the encode worker correctly uses `continue` — it marks the job as failed and moves on to the next one.

A single corrupted segment, transient disk error, or malformed frame dict permanently stops all metadata recording and finalization. In 24/7 operation this means hours of missing metadata from one bad frame.

## Affected files

- `flir/workers/metadata_workers.py` — `write_metadata_queue()` (line 176) and `metadata_finalize_queue()` (line 123)

## Current code

### Metadata writer — breaks on first error

```python
# metadata_workers.py:172-176, write_metadata_queue()

except Exception as exc:
    msg = f"metadata journal write failure: {exc}"
    tqdm.write(msg)
    set_worker_error(worker_error_state, msg)
    break                    # <-- entire worker exits, all subsequent frames lost
```

Any exception in the inner loop — `json.dumps()` on a weird frame, `out.write()` on a momentary I/O hiccup, `open()` on a transient permission error — kills the worker. Every frame after this point is silently discarded because the queue consumer is dead.

### Metadata finalizer — breaks on first error

```python
# metadata_workers.py:117-123, metadata_finalize_queue()

except Exception as exc:
    err = str(exc)
    msg = f"metadata finalize failure (job_id={job.job_id}): {err}"
    tqdm.write(msg)
    repo.mark_failed(conn, job.job_id, err)
    set_worker_error(worker_error_state, msg)
    break                    # <-- entire worker exits, all subsequent jobs abandoned
```

One corrupted `.metadata.jsonl` file (truncated line, invalid JSON) kills the finalizer. All pending and future finalize jobs are never processed — no `.json` files produced, no segment records enqueued.

### Encode worker — handles errors correctly (for comparison)

```python
# encode_worker.py:138-148, encode_jobs_worker()

except Exception as exc:
    err = str(exc)
    tqdm.write(f"encode job failed ({worker_id}, job_id={job.job_id}): {err}")
    repo.mark_failed(conn, job.job_id, err)
    # Clean up tmp file on failure
    tmp_out = job.output_mp4 + ".tmp.mp4"
    if os.path.exists(tmp_out):
        try:
            os.remove(tmp_out)
        except OSError:
            pass
    # NO break — continues to next job
```

The encode worker marks the job as failed and moves on. This is the correct pattern.

## How it happens

### Scenario 1: Transient disk I/O error kills metadata writer

1. NFS mount hiccups for 200ms during a write to `.metadata.jsonl`
2. `out.write(json.dumps(record))` raises `OSError`
3. `set_worker_error` fires, `break` exits the loop
4. NFS recovers 200ms later — but the metadata writer is dead
5. Capture loop continues recording for hours. All metadata for those hours is lost.
6. The `.metadata.jsonl` for the current segment is incomplete; finalize will produce truncated JSON

### Scenario 2: Corrupted metadata journal kills finalizer

1. A previous session crashed mid-write, leaving a `.metadata.jsonl` with a truncated final line
2. `finalize_legacy_json()` calls `json.loads(line)` on the truncated line → `json.JSONDecodeError`
3. Finalizer marks the job as failed and `break`s
4. All subsequent finalize jobs (from healthy segments) are never processed
5. No `.json` files, no segment records — the backend has no record of any segments after the bad one

### Scenario 3: Malformed frame dict

1. A PySpin update changes a chunk data field type, or a camera returns unexpected data
2. `build_metadata_journal_record(frame)` or `json.dumps(record)` raises
3. Metadata writer dies on that one frame. All subsequent frames for all segments are lost.

## Suggested fix

### Metadata writer: skip bad frames, don't kill the worker

```python
# metadata_workers.py, write_metadata_queue()

            except Exception as exc:
                msg = f"metadata journal write failure: {exc}"
                tqdm.write(msg)
                # Don't kill the worker for a single bad frame.
                # Only escalate if errors are persistent.
                continue
```

If you want to preserve the fail-fast behavior for truly fatal errors (disk full, permission denied), distinguish them:

```python
            except OSError as exc:
                # I/O errors are likely fatal (disk full, permissions).
                msg = f"metadata journal I/O failure: {exc}"
                tqdm.write(msg)
                set_worker_error(worker_error_state, msg)
                break
            except Exception as exc:
                # Data errors (bad frame, JSON encode) — skip and continue.
                tqdm.write(f"metadata journal write skipped ({exc})")
                continue
```

### Metadata finalizer: match the encode worker pattern

```python
# metadata_workers.py, metadata_finalize_queue()

            except Exception as exc:
                err = str(exc)
                msg = f"metadata finalize failure (job_id={job.job_id}): {err}"
                tqdm.write(msg)
                repo.mark_failed(conn, job.job_id, err)
                # Don't break — continue to next job, same as encode worker.
                continue
```

Remove `set_worker_error` from the finalizer entirely — a single bad segment's metadata should not halt acquisition.

## Impact without fix

| Duration | Segments lost | At 5-min segments, 30fps |
|----------|--------------|--------------------------|
| 1 hour | 12 segments | 216,000 frames of metadata |
| 8 hours | 96 segments | 1,728,000 frames of metadata |
| 24 hours | 288 segments | 5,184,000 frames of metadata |

All from one bad frame or one corrupted file.

## Related issues

- `queue.join()` deadlock (issue #1, fixed) — the dead metadata writer also caused shutdown hangs
- `queue.get()` no timeout (issue #2, fixed) — ensures the dead worker's thread eventually exits
- Failed jobs never retried (issue #14, open) — even after fixing this, `mark_failed` jobs stay stuck forever
