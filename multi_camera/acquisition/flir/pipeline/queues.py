from __future__ import annotations

import queue
from dataclasses import dataclass
from queue import Queue
import threading


@dataclass(frozen=True)
class QueuePolicy:
    maxsize: int
    mode: str  # "fail_fast" | "drop_on_full" | "best_effort"
    timeout_s: float = 0.0


@dataclass
class RecorderQueues:
    image_queues: dict[str, Queue]
    metadata_queue: Queue
    records_queue: Queue


def build_recorder_queues(camera_serials: list[str], frame_queue_size: int) -> RecorderQueues:
    image_queues = {serial: Queue(frame_queue_size) for serial in camera_serials}
    metadata_queue = Queue(frame_queue_size)
    # Per-segment summaries are low-frequency and should not block acquisition.
    records_queue = Queue()
    return RecorderQueues(image_queues=image_queues, metadata_queue=metadata_queue, records_queue=records_queue)


def set_worker_error(worker_error_state: dict | None, message: str):
    if worker_error_state is None:
        return
    worker_error_state["message"] = message
    event = worker_error_state.get("event")
    if event is not None:
        event.set()


def safe_put(q: Queue, item, queue_name: str | None = None):
    """Insert an item without blocking. Drops item when queue is full."""
    try:
        q.put_nowait(item)
    except queue.Full:
        if queue_name:
            print(f"{queue_name} full, dropping item")
        else:
            print("queue full, dropping item")


def put_metadata_or_fail(
    q: Queue,
    item: dict,
    timeout_s: float,
    worker_error_state: dict | None = None,
):
    """Metadata is sync-critical. Never silently drop when queue is full."""
    try:
        q.put(item, timeout=timeout_s)
    except queue.Full as exc:
        msg = f"CRITICAL: metadata queue full after {timeout_s:.2f}s; " "stopping acquisition to protect sync integrity"
        set_worker_error(worker_error_state, msg)
        raise RuntimeError(msg) from exc
