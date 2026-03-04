# Bug: Segment boundary fires on frame 0 — spurious empty first segment

## Problem

In continuous recording mode, the segment boundary condition is:

```python
if frame_idx % total_frames == 0:
```

`frame_idx` starts at 0, and `0 % N == 0` for any N. So the very first iteration triggers a segment reset — closing and reopening the progress bar, and generating a new `video_base_file` — before any frames have been captured.

This means the first segment's filename (set up by `_prepare_recording_target` before the capture loop) is immediately discarded and replaced. Any frames captured before the reset use one filename; the reset generates a different one. Depending on timing, the first frame may go to the original filename (creating a 1-frame segment) or all frames go to the new filename (orphaning the original).

## Affected file

- `flir/capture_runner.py` — `run_capture_loop()` (lines 50-64)

## Current code

```python
# capture_runner.py:22,34,50-64

frame_idx = 0                          # starts at 0
...
prog = tqdm(total=total_frames)
...
if recorder.camera_config["acquisition-type"] == "continuous":
    recorder.set_progress(frame_idx / total_frames)
    prog.update(1)                     # updates progress BEFORE boundary check

    # Reset progress and segment filename after each segment.
    if frame_idx % total_frames == 0:  # 0 % 9000 == 0 → TRUE on first iteration!
        prog.close()                   # closes the bar we just created
        prog = tqdm(total=total_frames)  # creates a new one immediately
        frame_idx = 0                  # resets to 0 (already 0)

        if recorder.video_base_file is not None:
            now = datetime.now()
            time_str = now.strftime("%Y%m%d_%H%M%S")
            recorder.video_base_name = "_".join([recorder.video_root, time_str])
            recorder.video_base_file = os.path.join(recorder.video_path, recorder.video_base_name)
```

At the bottom of the loop:

```python
# capture_runner.py:173
frame_idx += 1                         # increment happens AFTER the boundary check
```

## Walk-through of first 3 iterations

```
Iteration 1:
  frame_idx = 0
  prog.update(1)                → progress bar: 1/9000
  0 % 9000 == 0 → TRUE         → segment boundary fires!
    prog.close()                → bar closed after 1 update
    prog = tqdm(total=9000)     → new bar created (0/9000)
    frame_idx = 0               → no-op, already 0
    video_base_file = new path  → original filename discarded
  ... capture frame ...
  frame_idx += 1                → frame_idx = 1

Iteration 2:
  frame_idx = 1
  prog.update(1)                → progress bar: 1/9000
  1 % 9000 == 1 → FALSE        → no boundary, good
  ... capture frame ...
  frame_idx += 1                → frame_idx = 2

...

Iteration 9000:
  frame_idx = 8999
  prog.update(1)                → progress bar: 8999/9000 (never reaches 9000)
  8999 % 9000 == 8999 → FALSE  → no boundary
  ... capture frame ...
  frame_idx += 1                → frame_idx = 9000

Iteration 9001:
  frame_idx = 9000
  prog.update(1)                → progress bar: 9000/9000 (but immediately closed)
  9000 % 9000 == 0 → TRUE      → segment boundary fires (correct this time)
    prog.close()
    prog = tqdm(total=9000)
    frame_idx = 0
    video_base_file = new path
```

The first boundary at iteration 1 is spurious. The real boundary should only fire at iteration 9001.

## Impact

- First segment gets a different filename than intended, losing the initial `_prepare_recording_target` path
- Journal writers and metadata writers see a `base_filename` change on the very first frame, triggering an empty `_flush_journal_to_encode_job` (since `journal` is still `None`, this is a no-op — but it's still wrong)
- Progress bar flickers on startup (close + reopen immediately)
- If the initial `video_base_file` path was used by other code (e.g. to predict output paths), it's now stale

## Suggested fix

Move the increment before the boundary check, and check `frame_idx > 0`:

```python
if recorder.camera_config["acquisition-type"] == "continuous":
    frame_idx += 1
    recorder.set_progress(frame_idx / total_frames)
    prog.update(1)

    if frame_idx >= total_frames:
        prog.close()
        prog = tqdm(total=total_frames)
        frame_idx = 0

        if recorder.video_base_file is not None:
            now = datetime.now()
            time_str = now.strftime("%Y%m%d_%H%M%S")
            recorder.video_base_name = "_".join([recorder.video_root, time_str])
            recorder.video_base_file = os.path.join(recorder.video_path, recorder.video_base_name)
else:
    frame_idx += 1
    recorder.set_progress(frame_idx / max_frames)
    prog.update(1)
```

And remove the `frame_idx += 1` at the bottom of the loop (line 173), since it's now inside the branches.

This way:
- Frame 0 is captured, `frame_idx` becomes 1, `1 >= 9000` is false — no spurious boundary
- Frame 8999 is captured, `frame_idx` becomes 9000, `9000 >= 9000` is true — correct boundary
- Progress bar reaches `total_frames/total_frames` before closing — no flicker
