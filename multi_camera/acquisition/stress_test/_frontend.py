import atexit
import asyncio
import logging
import multiprocessing
import os
import signal
import subprocess
import time
import threading
from urllib.request import urlopen

import cv2
import numpy as np

log = logging.getLogger("flir_pipeline")

# Injected before page load to track blob URL creates/revokes with byte accounting.
_BLOB_TRACKER_JS = """
window.__blobStats = { creates: 0, revokes: 0, activeBytes: 0 };
const _blobSizes = new Map();
const _origCreate = URL.createObjectURL;
const _origRevoke = URL.revokeObjectURL;
URL.createObjectURL = function(obj) {
    const url = _origCreate.call(URL, obj);
    window.__blobStats.creates++;
    if (obj && obj.size) {
        _blobSizes.set(url, obj.size);
        window.__blobStats.activeBytes += obj.size;
    }
    return url;
};
URL.revokeObjectURL = function(url) {
    window.__blobStats.revokes++;
    const size = _blobSizes.get(url);
    if (size !== undefined) {
        window.__blobStats.activeBytes -= size;
        _blobSizes.delete(url);
    }
    _origRevoke.call(URL, url);
};
"""


def _websocket_server_process(preview_fps: float):
    """Mock backend — sends JPEG frames at the real preview rate (~fps/10)."""
    import uvicorn
    from fastapi import FastAPI, WebSocket
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    frame_width = 1080
    frame_height = int(1080 * (1200 / 1920))
    dummy = np.random.randint(0, 256, (frame_height, frame_width, 3), dtype=np.uint8)
    _, jpeg_data = cv2.imencode(".jpg", dummy)
    jpeg_bytes = jpeg_data.tobytes()

    @app.websocket("/api/v1/video_ws")
    async def video_ws(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_bytes(jpeg_bytes)
                await asyncio.sleep(1.0 / preview_fps)
        except Exception:
            pass

    @app.websocket("/api/v1/ws/{client_id}")
    async def status_ws(websocket: WebSocket, client_id: str):
        """Mock status WebSocket — the frontend expects this alongside video_ws."""
        await websocket.accept()
        try:
            while True:
                await asyncio.sleep(60)
        except Exception:
            pass

    # Stub REST endpoints so React doesn't fill the console with 404s on mount.
    @app.get("/api/v1/session")
    async def session():
        return JSONResponse({"session_id": "stress_test", "status": "idle"})

    @app.get("/api/v1/camera_status")
    async def camera_status():
        return JSONResponse([])

    @app.get("/api/v1/current_config")
    async def current_config():
        return JSONResponse({"config": {}})

    @app.get("/api/v1/configs")
    async def configs():
        return JSONResponse([])

    @app.get("/api/v1/prior_recordings")
    async def prior_recordings():
        return JSONResponse([])

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


def _wait_for_http(url: str, timeout: float = 90.0, interval: float = 2.0) -> bool:
    """Poll *url* until it returns HTTP 200 or *timeout* is exceeded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = urlopen(url, timeout=5)
            if r.status == 200:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


class FrontendMonitor:
    """Monitors frontend memory via headless Chromium against the React app.

    Tracks three categories of leaks:
      - Blob URL leaks (createObjectURL without revokeObjectURL) via JS injection
      - JS heap growth via CDP Performance.getMetrics
      - DOM node / event listener growth via CDP Performance.getMetrics
    """

    def __init__(self, fps: float, sample_interval_s: float = 10.0):
        self.fps = fps
        self.preview_fps = max(1.0, fps / 10.0)
        self.sample_interval_s = sample_interval_s
        self.ws_proc = None
        self.npm_proc = None
        self._pw_thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.failed = False
        # Blob URL tracking
        self.blob_creates = 0
        self.blob_revokes = 0
        self.blob_active_bytes = 0
        # Full samples: (elapsed_s, blob_leaked_mb, js_heap_mb, dom_nodes)
        self.samples = []
        self.start_time = 0
        # Legacy compatibility — used by growth_rate_mb_per_min and __main__.py reporting
        self.rss_samples = []
        # Ensure subprocesses die even if stop() is never called
        atexit.register(self._force_kill)

    def start(self):
        log.info("  Starting frontend mock backend (WebSocket + REST)...")
        self.ws_proc = multiprocessing.Process(target=_websocket_server_process, args=(self.preview_fps,), daemon=True)
        self.ws_proc.start()

        log.info("  Waiting for mock backend (port 8000)...")
        if not _wait_for_http("http://localhost:8000/api/v1/session", timeout=30):
            log.error("  Mock backend did not start within 30s")
            self.failed = True
            self.stop()
            return

        log.info("  Starting React frontend (npm start)...")
        env = os.environ.copy()
        env["BROWSER"] = "none"
        env["PORT"] = "3000"
        self.npm_proc = subprocess.Popen(
            ["npm", "start", "--silent"],
            cwd="/Mocap/react_frontend",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )

        log.info("  Waiting for React dev server (port 3000)...")
        if not _wait_for_http("http://localhost:3000", timeout=90):
            log.error("  React dev server did not start within 90s")
            self.failed = True
            self.stop()
            return

        # Playwright's sync API uses greenlets — all calls must stay on one thread.
        self._pw_thread = threading.Thread(target=self._playwright_loop, daemon=True, name="frontend_playwright")
        self._pw_thread.start()
        self._ready.wait(timeout=30)

    def _playwright_loop(self):
        """Dedicated thread: browser init, page navigation, sampling loop, cleanup."""
        browser = None
        pw = None
        try:
            from playwright.sync_api import sync_playwright

            log.info("  Launching headless Chromium via Playwright...")
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--enable-precise-memory-info"],
            )
            page = browser.new_page()
            page.add_init_script(_BLOB_TRACKER_JS)
            page.goto("http://localhost:3000/")

            cdp = page.context.new_cdp_session(page)
            cdp.send("Performance.enable")

            page.wait_for_timeout(5000)

            self.start_time = time.monotonic()
            self._ready.set()
            log.info("  Headless browser attached — monitoring frontend memory.")

            while not self._stop.wait(self.sample_interval_s):
                self._do_sample(page, cdp)
            self._do_sample(page, cdp)

        except ImportError:
            log.error("  Playwright not installed!")
            self.failed = True
            self._ready.set()
        except Exception as e:
            log.error(f"  Frontend monitor failed: {e}")
            self.failed = True
            self._ready.set()
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    pw.stop()
                except Exception:
                    pass

    def _do_sample(self, page, cdp):
        """Called from the Playwright thread only."""
        try:
            elapsed = time.monotonic() - self.start_time

            # Blob URL stats from injected JS
            blob = page.evaluate("window.__blobStats || {creates:0, revokes:0, activeBytes:0}")
            self.blob_creates = blob.get("creates", 0)
            self.blob_revokes = blob.get("revokes", 0)
            self.blob_active_bytes = blob.get("activeBytes", 0)
            blob_leaked_mb = self.blob_active_bytes / (1024 * 1024)

            # CDP Performance.getMetrics — JS heap, DOM nodes, event listeners
            raw = cdp.send("Performance.getMetrics")
            metrics = {m["name"]: m["value"] for m in raw.get("metrics", [])}
            js_heap_mb = metrics.get("JSHeapUsedSize", 0) / (1024 * 1024)
            dom_nodes = int(metrics.get("Nodes", 0))

            self.samples.append((elapsed, blob_leaked_mb, js_heap_mb, dom_nodes))
            self.rss_samples.append((elapsed, blob_leaked_mb))
        except Exception as e:
            log.debug(f"Failed to sample browser: {e}")

    def sample(self):
        """No-op — sampling is handled by _playwright_loop on its dedicated thread."""
        pass

    def stop(self):
        self._stop.set()
        if self._pw_thread:
            self._pw_thread.join(timeout=15)
        self._force_kill()

    def _force_kill(self):
        """Kill subprocesses unconditionally. Safe to call multiple times."""
        if self.npm_proc and self.npm_proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.npm_proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        if self.ws_proc and self.ws_proc.is_alive():
            self.ws_proc.kill()
            self.ws_proc.join(timeout=3)

    # -- Derived metrics -------------------------------------------------------

    @property
    def growth_rate_mb_per_min(self) -> float:
        """Blob URL leak rate in MB/min."""
        if len(self.rss_samples) < 2:
            return 0.0
        first_t, first_mb = self.rss_samples[0]
        last_t, last_mb = self.rss_samples[-1]
        elapsed_min = (last_t - first_t) / 60.0
        if elapsed_min < 0.5:
            return 0.0
        return (last_mb - first_mb) / elapsed_min

    @property
    def js_heap_growth_mb_per_min(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        first_t, _, first_heap, _ = self.samples[0]
        last_t, _, last_heap, _ = self.samples[-1]
        elapsed_min = (last_t - first_t) / 60.0
        if elapsed_min < 0.5:
            return 0.0
        return (last_heap - first_heap) / elapsed_min

    @property
    def dom_node_growth_per_min(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        first_t, _, _, first_nodes = self.samples[0]
        last_t, _, _, last_nodes = self.samples[-1]
        elapsed_min = (last_t - first_t) / 60.0
        if elapsed_min < 0.5:
            return 0.0
        return (last_nodes - first_nodes) / elapsed_min
