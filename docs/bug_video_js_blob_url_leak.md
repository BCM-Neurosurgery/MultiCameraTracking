# Bug: Video.js Blob URL Memory Leak

**File:** `react_frontend/src/components/Video.js`
**Status:** Confirmed by automated test, not yet fixed
**Severity:** High — caused the 2026-04-03 OOM crash and 5.4-hour recording gap
**Related:** `docs/incident_20260403_oom_crash.md` (full incident report)

---

## The Bug

The Video component receives JPEG frames over WebSocket and displays them as an `<Image>` element. Each frame goes through:

1. `new Blob([data])` — wraps the raw bytes
2. `URL.createObjectURL(blob)` — creates a URL the browser can render
3. `setImageSrc(url)` — React displays the image

The problem is step 2: every `createObjectURL` call allocates memory that is **never freed** because `revokeObjectURL` is never called.

### Why revokeObjectURL Never Runs

```javascript
// Video.js lines 13-34
useEffectOnce(() => {
    ws.current.onmessage = (event) => {
        if (imageSrc) {                        // ← BUG: always ""
            URL.revokeObjectURL(imageSrc);     // ← never executes
        }
        const blob = new Blob([event.data], { type: "image/jpeg" });
        const url = URL.createObjectURL(blob);
        setImageSrc(url);
    };
}, []);
```

`useEffectOnce` with `[]` deps creates the `onmessage` handler once at mount. JavaScript closures capture variables by reference to their scope — but `imageSrc` is a React state variable created by `useState("")`. The value inside the closure is frozen at the initial value `""` and never updates, even though `setImageSrc(url)` updates React's internal state for rendering.

This is a well-known React pitfall: **state variables inside effect callbacks become stale** when the dependency array doesn't include them. The `if (imageSrc)` check evaluates `if ("")` on every single frame — always false.

The cleanup function (lines 44-51) has the same stale closure problem — it also captures `imageSrc` as `""`.

### Leak Rate

The real backend sends a preview frame every 10th camera frame. At 30 FPS camera rate, that's ~3 preview frames/second reaching the browser.

Each preview JPEG is ~150 KB (1080-wide composited camera grid, JPEG-compressed).

| Time open | Blob URLs leaked | Memory consumed |
|-----------|-----------------|-----------------|
| 1 minute  | 180             | ~27 MB          |
| 1 hour    | 10,800          | ~1.6 GB         |
| 8 hours   | 86,400          | ~12.7 GB        |
| 24 hours  | 259,200         | ~38 GB          |

The system has 60 GB RAM. The leak becomes fatal somewhere between 8-24 hours depending on what else is running.

---

## Test Reproduction

On 2026-04-03, `make validate DURATION=120` confirmed the leak. The test launches headless Chromium against the React app, injects counters on `URL.createObjectURL` and `URL.revokeObjectURL`, and monitors the difference.

### Test output (2-minute soak)

```
Blob URLs          353 leaked at 139 MB/min     ✗
Frontend Heap      6→7 MB (stable)              ✓
```

### Key numbers from `report.json`

```json
{
  "frontend_blob_leaked": 353,
  "frontend_blob_growth_mb_per_min": 139.48,
  "frontend_heap_growth_mb_per_min": 0.25
}
```

- **353 blob URLs created, 0 revoked** — confirms `revokeObjectURL` is never called
- **139 MB/min** leak rate — extrapolates to **~8.3 GB/hour**
- **JS heap stable at 6-7 MB** — the leaked data lives in the browser's native memory, outside the V8 JS heap. This is why `window.performance.memory.usedJSHeapSize` cannot detect this leak.

### Connection to the incident

The OOM crash killed a Firefox process with 18.7 GB RSS. At 139 MB/min, reaching 18.7 GB takes ~134 minutes (~2.2 hours) of having the acquisition page open. The recording ran for ~39 hours total; the browser didn't need to be open the entire time — just long enough for someone to check the preview and leave the tab open.

---

## Fix

Replace the stale state variable with a `useRef`, which is a mutable container whose `.current` property always reflects the latest value regardless of closure scope:

```javascript
const prevUrlRef = useRef(null);

ws.current.onmessage = (event) => {
    if (prevUrlRef.current) {
        URL.revokeObjectURL(prevUrlRef.current);
    }
    const blob = new Blob([event.data], { type: "image/jpeg" });
    const url = URL.createObjectURL(blob);
    prevUrlRef.current = url;
    setImageSrc(url);
};

// Cleanup on unmount:
return () => {
    if (ws.current) ws.current.close();
    if (prevUrlRef.current) URL.revokeObjectURL(prevUrlRef.current);
};
```

Also remove the `console.log("new image")` on line 24, which logs on every frame and causes Firefox to retain console entries in memory.

### Verification

After fixing, `make validate` should show:

```
Blob URLs          353 created, all revoked     ✓
```
