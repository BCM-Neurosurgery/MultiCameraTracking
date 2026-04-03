# Incident Report: OOM Crash and 5-Hour Recording Gap

**Date:** 2026-04-03
**Affected session:** TRBD003, cameras 24253448/24253450/24253452/24253459/24253460/24253466
**Data loss window:** ~00:21 to ~05:47 CDT (~5.4 hours)
**Investigated by:** nbusleep + Claude Code, 2026-04-03

---

## 1. Timeline of Events

### Normal operation (through 00:20)

- Recording session started 2026-04-01 ~09:46 CDT, running continuously with 6 cameras.
- 10-minute segments (~18,000 frames at 30fps) completing normally.
- Last fully completed segment: `TRBD003_20260402_235101` (6 cams, 18,000 frames).
- Next segment `TRBD003_20260403_000111` started at 00:01:11 CDT.
- Docker container `b7ec3970cac6` had been running for ~4 days 23 hours.

### OOM crash (00:21)

- **00:21:10** — Kernel logs `python3: page allocation failure: order:0, mode:0x820(GFP_ATOMIC)` inside Docker container `b7ec3970cac6`.
- The failure occurs in the `atlantic` network driver (Aquantia 10GbE NIC on `enp17s0`) in `__napi_alloc_skb` — the kernel cannot allocate memory for incoming camera network packets.
- **00:21:11** — `reaper/audio` invokes OOM killer. Kernel kills Firefox process `Isolated Web Co` (PID 631199), which had:
  - `anon-rss: 18,711,888 kB` (~18.7 GB)
  - `total-vm: 65,925,616 kB` (~63 GB virtual)
  - `oom_score_adj: 200` (browser processes get penalized)

**System memory state at OOM:**

| Resource | Value |
|----------|-------|
| Total RAM | 60 GB |
| Active anon | 52.2 GB |
| Free (Normal zone) | 22 MB (below 90 MB min watermark) |
| Swap total | 2 GB |
| Swap free | 244 KB |

**Key processes in OOM dump:**

| Process | PID | RSS (pages) | Note |
|---------|-----|-------------|------|
| `Isolated Web Co` (Firefox tab) | 631199 | 18,711,888 kB | OOM-killed |
| `python3` (recording pipeline) | 495659 | ~685 MB | In Docker container |
| `npm start` (React frontend) | 495660 | ~9 MB | Frontend server was running |
| `start_flir_reco` | 495485 | ~1.7 MB | Recording launcher |
| `dockerd` | 2096 | ~45 MB | |

### Recording death (00:21:11–00:21:22)

Application log (`~/data/TRBD-53761/TRBD003/VIDEO/20260401/TRBD003_20260401_094647.log`):

```
00:21:12 WARNING 24253452: failed to get image, streak 1 (GenTL error: -1011)
00:21:13 WARNING 24253459: failed to get image, streak 1 (GenTL error: -1011)
00:21:14 WARNING 24253460: failed to get image, streak 1 (GenTL error: -1011)
00:21:15 WARNING 24253466: failed to get image, streak 1 (GenTL error: -1011)
00:21:16 WARNING 24253448: Image incomplete | Image has missing packets
00:21:17 WARNING 24253450: failed to get image, streak 1 (GenTL error: -1011)
00:21:22 WARNING 24253452: Camera has been removed from the list [-1024]
00:21:22 WARNING 24253459: Camera has been removed from the list [-1024]
  ... (all 6 cameras report "removed", streaks 10, 20, 30)
```

The log ends abruptly at streak 30. No shutdown message, no error summary. The process hung.

Metadata file (`TRBD003_20260403_000111.metadata.jsonl`, 14,745 lines) confirms:
- Line 14715 (00:21:11.421 UTC): 6 cameras, last good frame
- Line 14716 (00:21:11.430 UTC): 2 cameras — 9ms later
- Line 14717 (00:21:15.450 UTC): 0 cameras
- Lines 14718–14745: empty frames, then writer hung

**Result:** Segment `000111` produced 6 MP4 files (~1 GB each) but **no JSON metadata file** — the metadata finalizer never ran.

### Zombie gap (00:22 to 05:27)

- Docker container `b7ec3970cac6` stayed alive but produced no output.
- System logs show nothing relevant between 00:22 and 05:25 (only routine cron jobs, VPN routing updates).
- **~5 hours of completely silent failure.**

### Recovery attempts (05:25 to 05:47)

Coworker arrived and began troubleshooting:

| Time | Action | Recording produced |
|------|--------|--------------------|
| 05:27:33 | Force-killed zombie container `b7ec` (didn't respond to SIGTERM within 10s) | — |
| 05:27:40 | Started container `89fc` + `set_mtu.sh` | `052808`: 4 cams, 415 frames (14s) |
| 05:29:09 | Container `89fc` died (1.5 min lifespan) | — |
| 05:29:28 | Started container `c86b` + `set_mtu.sh` | `053056`: 3 cams, 29 frames (1s) |
| 05:34:38 | Container `c86b` force-killed | — |
| 05:34:59 | Started container `afe4` + `set_mtu.sh` | `053720`: 2 cams, 5 frames |
| 05:37:44 | Container `afe4` died | — |
| 05:38:06 | Started container `5681` + `set_mtu.sh` | `053836`: 6 cams, 7164 frames (but cam 24253460 only 206 KB) |
| | | `054242`: 5 cams, 29 frames |
| 05:46:47 | Container `5681` died | — |
| 05:47:02 | Started container `780a` + `set_mtu.sh` | `054751` onwards: **6 cams, stable** |

Camera count fluctuation (4→3→2→6→5→6) is because GigE Vision camera handles from the force-killed container were not cleanly released. Each restart found a different subset of cameras the Spinnaker SDK could initialize.

### Stable operation resumed (05:47 onwards)

From `054751` onwards: 6 cameras, 18,000 frames per segment, normal file sizes (~900 MB–1.1 GB per camera per segment).

---

## 2. Root Cause

**Immediate cause:** System ran out of memory (60 GB RAM + 2 GB swap fully consumed), causing the `atlantic` NIC driver to fail allocating socket buffers for incoming GigE Vision camera packets.

**Primary memory consumer:** A Firefox process (`Isolated Web Co`) using 18.7 GB RSS.

**What exactly did the OOM killer do?** The kernel killed **one Firefox tab process** (PID 631199) — nothing else. The recording pipeline was NOT directly OOM-killed. It died as collateral damage: during the ~1 second window when memory was fully exhausted, the `atlantic` NIC driver could not allocate socket buffers (`__napi_alloc_skb` failed with `GFP_ATOMIC`), so incoming camera packets were dropped at the kernel network layer. Once the Spinnaker SDK loses camera connections (`GenTL error -1011` → `Camera removed [-1024]`), recovery is not possible — the pipeline hung.

Other processes (e.g., `reaper/audio`) survived because audio streaming uses far smaller buffers and lower bandwidth than 6 cameras over 10GbE. The audio device can also re-sync after brief interruptions; GigE Vision cameras cannot.

**What is `Isolated Web Co`?** It's Firefox's name for a sandboxed content process that renders a tab. These processes **only exist while Firefox is running** with tabs open. Close Firefox → all content processes exit → memory freed. There is no residual risk when Firefox is closed.

**What we could NOT determine:** Which specific Firefox tab consumed the 18.7 GB. The kernel OOM log only records process name and PID — Firefox does not label content processes with the tab URL (unlike Chrome, which does). The killed session's state was lost, and the `previous.jsonlz4` session backup (from the post-restart session) only shows Slack tabs. There is no way to retroactively determine this.

**What we DO know:**
- `npm start` (the React frontend server) was running at the time of the OOM.
- Firefox history shows `http://localhost:3000/` has 164 visits — the most-visited localhost URL.
- The frontend code (`Video.js`) has a **proven** Blob URL memory leak (see Issue F1 below).
- The user confirmed that Slack was 100% NOT open in Firefox, and that the only open tab was Notion.
- Any of these tabs (or a combination) could have been the 18.7 GB consumer.

**Contributing factors:**
- No memory isolation between Firefox and the recording Docker container.
- No watchdog or alerting — pipeline hung silently for 5+ hours.
- Application log was in the raw data directory, not alongside the sorted video data, making it hard to find during troubleshooting.

---

## 3. Identified Issues

### F1. Video.js Blob URL leak (CONFIRMED BUG)

**File:** `react_frontend/src/components/Video.js:23-34`

The `onmessage` WebSocket handler uses a stale closure over `imageSrc`. Because `useEffectOnce` runs once with `[]` deps, the closure captures the initial state value `""`. `setImageSrc(url)` updates React state for rendering, but the `imageSrc` variable inside the closure **never changes** — it is always `""`. Therefore `URL.revokeObjectURL(imageSrc)` on line 28 is always a no-op.

**Every Blob URL created is leaked. This is provable from the code — it is not an assumption.**

At 30fps, this would leak ~2.6 million Blob URLs per day if the video preview is actively streaming.

**Severity:** High — but only leaks while a recording is active AND the Video Preview component is mounted in the browser.

### F2. Video.js `console.log("new image")` on every frame

**File:** `react_frontend/src/components/Video.js:24`

Logs a string on every WebSocket frame (~30fps). Firefox retains console entries in memory even without DevTools open.

### F3. Viewer.js `close()` does not dispose Three.js resources

**File:** `react_frontend/src/components/visualization_js/viewer.js:442-448`

`close()` only removes DOM elements. Missing:
- `this.renderer.dispose()` — WebGL context stays allocated
- Scene traversal to dispose geometries, materials, textures
- `this.controls.dispose()` — OrbitControls event listeners on renderer DOM
- `this.gui.destroy()` — lil-gui cleanup
- `this.animator` cleanup — `requestAnimationFrame` loop keeps running forever
- `ResizeObserver` (line 260) — never `disconnect()`ed, prevents GC of Viewer
- `window.addEventListener('resize', ...)` (line 257) — never removed
- `window.onload = ...` (line 256) — overwrites global, captures Viewer reference

**Severity:** Medium — leaks GPU memory and CPU (animation loop) each time a user switches recordings in Annotator, SmplBrowser, or BiomechanicsBrowser.

### F4. Selector.js event listeners never removed

**File:** `react_frontend/src/components/visualization_js/selector.js:17-20`

Adds `pointermove`/`pointerdown`/`pointerup` listeners with `.bind(this)` but has no `dispose()` method. Bound functions are anonymous, so they can't be removed even if the caller wanted to.

**Severity:** Low — only matters if Viewer instances are created/destroyed repeatedly.

### P1. No per-segment or rotated logging

**File:** `multi_camera/acquisition/flir/logging_setup.py`

One `FileHandler` created per session, never rotated. A 5-day session produces one log file in the raw data directory. The external sorter program moves MP4s to `VIDEO_DATA_SORTED/` but leaves the log behind, making it hard to find.

### P2. No watchdog or alerting for silent pipeline hangs

The capture loop logged "Camera removed" warnings at 00:21:22, then went completely silent for 5 hours. No heartbeat, no health check, no external notification. The pipeline does not self-terminate when all cameras are lost.

### P3. No memory isolation between host processes and recording container

Firefox (running on the host) consumed memory from the same pool as the Docker container. No `mem_limit` or cgroup protection on either side.

### P4. `make validate` does not test frontend memory

The stress test monitors Python pipeline RSS (`/proc/self/status VmRSS`) and catches backend leaks. But it never starts a browser or frontend, so frontend memory leaks are invisible to the validation process.

### P5. Videos have a green tint in QuickTime/thumbnails

**File:** `multi_camera/acquisition/flir/workers/encoder_worker.py`

The encoding pipeline produces H.264 video with the color stored in an unusual format called GBR 4:4:4 (Green-Blue-Red planes) instead of the standard YUV that most software expects. VLC displays it correctly by reading the metadata tag, but QuickTime and file explorer thumbnails assume standard YUV, interpreting the Green plane as brightness.

---

## 4. Proposed Fixes

### Fix F1: Video.js Blob URL leak

Use a ref to track the previous URL instead of relying on the stale closure:

```js
const prevUrlRef = useRef(null);

ws.current.onmessage = (event) => {
    const blob = new Blob([event.data], { type: "image/jpeg" });
    const url = URL.createObjectURL(blob);
    if (prevUrlRef.current) {
        URL.revokeObjectURL(prevUrlRef.current);
    }
    prevUrlRef.current = url;
    setImageSrc(url);
};

// In cleanup:
return () => {
    if (ws.current) ws.current.close();
    if (prevUrlRef.current) URL.revokeObjectURL(prevUrlRef.current);
};
```

### Fix F2: Remove console.log per frame

Delete line 24 (`console.log("new image")`).

### Fix F3: Viewer.js proper disposal

Rewrite `close()` to:
1. Set a `_closed` flag and guard `animate()` with `if (this._closed) return`
2. Call `this.renderer.dispose()`
3. Traverse `this.scene` and dispose all geometries/materials/textures
4. Call `this.controls.dispose()`
5. Call `this.gui.destroy()`
6. Disconnect the `ResizeObserver` (requires storing it as `this._resizeObserver` in constructor)
7. Remove the `window` resize listener (requires storing bound handler as `this._onResize`)
8. Call `this.selector.dispose()`

### Fix F4: Selector.js dispose method

Store bound handlers as instance properties, add `dispose()`:

```js
constructor(viewer) {
    super();
    // ...
    this._onPointerMove = this.onPointerMove.bind(this);
    this._onPointerDown = this.onPointerDown.bind(this);
    this._onPointerUp = this.onPointerUp.bind(this);
    domElement.addEventListener('pointermove', this._onPointerMove);
    domElement.addEventListener('pointerdown', this._onPointerDown);
    domElement.addEventListener('pointerup', this._onPointerUp);
}

dispose() {
    const domElement = this.viewer.domElement;
    domElement.removeEventListener('pointermove', this._onPointerMove);
    domElement.removeEventListener('pointerdown', this._onPointerDown);
    domElement.removeEventListener('pointerup', this._onPointerUp);
}
```

### Fix P1: Log rotation + log co-location with sorted data

**Option A (recommended):** Keep one log per session, but:
- The external data sorter should copy/symlink the session log alongside the sorted video data.
- Add size-based rotation (`RotatingFileHandler`, 50 MB, keep 5 backups) as a safety net.

**Option B:** Rotate the log file at each segment boundary. Provides per-segment logs but loses cross-segment context for debugging.

### Fix P2: Watchdog and alerting

1. **Capture loop heartbeat:** Write a timestamp to a watchdog file (e.g., `/tmp/flir_recording_alive`) every 30 seconds.
2. **Max failure streak auto-shutdown:** If all cameras report "removed" for >60 seconds, trigger graceful shutdown (flush journals, finalize metadata) instead of hanging forever.
3. **External watchdog:** A cron job or systemd timer that checks the watchdog file mtime. If stale by >2 minutes during an active recording, send alert (email, Slack webhook, etc.).
4. **Backend WebSocket alert:** If the recording is supposed to be active but no frames arrive for >60 seconds, push an `"alert"` status to connected frontend clients.

### Fix P3: Docker memory isolation

Add to `docker-compose.yml` for the recording container:

```yaml
mem_limit: 20g       # or appropriate limit for 6-camera recording
memswap_limit: 20g   # no swap — fail fast rather than degrade
```

This creates a cgroup boundary. Host processes (Firefox, Slack, etc.) cannot starve the container.

### Fix P4: Frontend memory in stress test

Add a `--with-frontend` mode to `make validate` that:
1. Starts the FastAPI backend
2. Launches headless Chromium pointed at `localhost:3000` with Video Preview active
3. Samples browser RSS via `psutil` or Chrome DevTools Protocol
4. Reports browser memory growth rate alongside pipeline memory
5. Fails if browser growth > 50 MB/min
Could be a separate target: `make validate-frontend`.

### Fix P5: Standardize video encoding format

Update `_build_ffmpeg_cmd` in `encoder_worker.py` to add `-pix_fmt yuv420p` when invoking `libx264` or `h264_nvenc`, ensuring maximum compatibility with standard players and thumbnails.

---

## 5. TODOs

### Immediate (before next overnight recording)

- [ ] **Tell data collectors:** Close Firefox/browser tabs when not actively checking the UI. The leak only runs while the page is open and receiving video frames.
- [ ] **Verify F1 leak empirically:** Start a short recording (~5 min), open `localhost:3000` in Firefox with DevTools Memory tab, observe Blob count / JS heap growing linearly. This confirms the leak is active in practice, not just in theory.

### Short-term fixes

- [ ] **Fix F1** — Video.js Blob URL stale closure (the proven bug)
- [ ] **Fix F2** — Remove `console.log("new image")`
- [ ] **Fix P5** — Standardize video encoding format to fix green tint
- [ ] **Implement P2 (partial)** — Add max failure streak auto-shutdown so pipeline doesn't hang when all cameras are lost
- [ ] **Implement P3** — Add `mem_limit` to Docker compose for the recording container

### Medium-term fixes

- [ ] **Fix F3** — Viewer.js proper `close()` disposal
- [ ] **Fix F4** — Selector.js `dispose()` method
- [ ] **Implement P1** — Log co-location with sorted data (update the sorter script)
- [ ] **Implement P2 (full)** — External watchdog + alerting mechanism

### Longer-term improvements

- [ ] **Implement P4** — Add frontend memory monitoring to stress test
- [ ] **Investigate:** Was the 18.7 GB Firefox tab actually viewing `localhost:3000` or Slack or something else? Verification steps above will help answer whether the frontend leak is a practical concern at the current streaming rate.

---

## 6. Evidence Locations

| Item | Path |
|------|------|
| Application log (session) | `~/data/TRBD-53761/TRBD003/VIDEO/20260401/TRBD003_20260401_094647.log` |
| Orphaned metadata | `~/data/TRBD-53761/TRBD003/VIDEO/20260401/TRBD003_20260403_000111.metadata.jsonl` |
| Sorted video data | `~/data/VIDEO_DATA_SORTED/TRBD-53761/TRBD003/2026-04-03/` |
| Kernel OOM logs | `journalctl --since "2026-04-03 00:21:00" --until "2026-04-03 00:21:30"` |
| Container lifecycle | `journalctl --since "2026-04-03 05:27" --until "2026-04-03 06:00" \| grep docker` |
| Firefox history | `~/snap/firefox/common/.mozilla/firefox/fet5gjhe.default/places.sqlite` (query: `WHERE url LIKE '%localhost%'`) |
| Frontend code (Video.js) | `react_frontend/src/components/Video.js` |
| Frontend code (Viewer.js) | `react_frontend/src/components/visualization_js/viewer.js` |
| Frontend code (Selector.js) | `react_frontend/src/components/visualization_js/selector.js` |
| Logging setup | `multi_camera/acquisition/flir/logging_setup.py` |
| Stress test | `multi_camera/acquisition/stress_test/__main__.py` |
