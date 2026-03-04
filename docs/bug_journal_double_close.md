# Bug: `SegmentJournalWriter.close()` double-close crashes during error cleanup

## Problem

`_flush_journal_to_encode_job` calls `journal.close()`, which calls `os.fsync()` and `_fh.close()`. If this function is called twice on the same journal object, the second call crashes with `ValueError: I/O operation on closed file` (from `os.fsync` on a closed fd) or `OSError: [Errno 9] Bad file descriptor`.

This happens when an exception occurs during `_flush_journal_to_encode_job` at a segment boundary — the outer `finally` block calls it again on the same journal.

## Affected file

- `flir/workers/journal_writer_worker.py` — `write_journal_queue()` (lines 114, 129) and `SegmentJournalWriter.close()` (lines 46-49)

## How it happens

```python
# journal_writer_worker.py — the two call sites

try:
    while True:
        ...
        try:
            ...
            if base_filename != current_base:
                _flush_journal_to_encode_job(repo, journal, acquisition_fps)  # line 114
                #   ↑ calls journal.close() at line 60
                #   ↑ then repo.enqueue_job() at line 68 — CAN FAIL
                current_base = base_filename
                journal = SegmentJournalWriter(...)  # line 116 — never reached
        except Exception as exc:
            ...
            break                    # exits loop, journal still points to CLOSED object
        finally:
            image_queue.task_done()
finally:
    _flush_journal_to_encode_job(repo, journal, acquisition_fps)  # line 129
    #   ↑ journal is NOT None (still the old, closed object)
    #   ↑ calls journal.close() AGAIN → crash
    flush_done_event.set()
```

### Concrete timeline

```
1. Segment boundary detected (base_filename changes)
2. _flush_journal_to_encode_job(repo, journal, fps)  ← line 114
   2a. journal.close()                                 ← succeeds, file closed
   2b. repo.enqueue_job(...)                           ← FAILS (SQLite locked)
   2c. Exception propagates out of _flush_journal_to_encode_job
3. except Exception catches it
   3a. worker_error_state set
   3b. break
4. finally: image_queue.task_done()
5. Outer finally:
   _flush_journal_to_encode_job(repo, journal, fps)   ← line 129
   5a. journal is not None (still the old object)
   5b. journal.close()                                 ← CRASH: fd already closed
       → ValueError: I/O operation on closed file
```

The crash in step 5b masks the original SQLite error from step 2b. The `flush_done_event.set()` at line 130 is never reached, so `stop_workers` hangs on `event.wait(timeout=30)` for 30 seconds.

## A second path: `_fh.write()` failure

Even without the segment boundary, if `_fh.write()` fails mid-frame (disk full):

```
1. journal.write_frame() raises OSError at line 42 (_fh.write)
2. except catches it, break
3. Outer finally: _flush_journal_to_encode_job(repo, journal, fps)
   3a. journal.close()
       → _fh.flush()   ← may also fail (disk full)
       → os.fsync()    ← may fail
       → _fh.close()   ← succeeds (closing is always allowed)
```

This path doesn't double-close, but `_fh.flush()` on a full disk will raise, and the `flush_done_event.set()` is again never reached.

## Suggested fix

Add a `_closed` guard to `SegmentJournalWriter.close()`:

```python
class SegmentJournalWriter:
    def __init__(self, base_filename: str, serial: str):
        ...
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
```

And wrap the outer finally to ensure `flush_done_event` is always set:

```python
finally:
    try:
        _flush_journal_to_encode_job(repo, journal, acquisition_fps)
    except Exception as exc:
        tqdm.write(f"flush error during cleanup ({serial}): {exc}")
    flush_done_event.set()
```

## Related issues

- Orphaned `.journal` files (issue #16) — if `enqueue_job` fails after `close()`, the journal is orphaned. The double-close fix doesn't address this, but protecting `flush_done_event.set()` prevents the 30s shutdown hang.
