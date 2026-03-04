"""Metadata worker loops: journal writes and legacy JSON finalization."""

from __future__ import annotations

from datetime import datetime
import json
import queue
from queue import Queue
import threading
import time

from tqdm import tqdm

from multi_camera.acquisition.flir.pipeline.messages import MetadataPacket, SegmentRecord
from multi_camera.acquisition.flir.pipeline.queues import set_worker_error
from multi_camera.acquisition.flir.storage.finalize_jobs_repo import FinalizeJobsRepo


def build_metadata_journal_record(frame: dict) -> dict:
    packet = MetadataPacket.from_frame_dict(frame)
    return packet.to_journal_record()


def finalize_legacy_json(base_filename: str, config_metadata: dict, recording_timestamp: datetime, records_queue: Queue):
    """Build legacy segment JSON from append-only per-frame metadata journal."""
    journal_file = base_filename + ".metadata.jsonl"
    json_file = base_filename + ".json"
    tmp_file = json_file + ".tmp"

    json_data = {
        "real_times": [],
        "timestamps": [],
        "frame_id": [],
        "frame_id_abs": [],
        "chunk_serial_data": [],
        "serial_msg": [],
        "serials": [],
        "camera_config_hash": config_metadata["camera_config_hash"],
        "camera_info": config_metadata["camera_info"],
        "meta_info": config_metadata["meta_info"],
        "exposure_times": [],
        "frame_rates_requested": [],
        "frame_rates_binning": [],
    }

    last_row = None
    with open(journal_file, "r") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            last_row = row
            json_data["real_times"].append(row["real_times"])
            json_data["timestamps"].append(row["timestamps"])
            json_data["frame_id"].append(row["frame_id"])
            json_data["frame_id_abs"].append(row["frame_id_abs"])
            json_data["chunk_serial_data"].append(row["chunk_serial_data"])
            json_data["serial_msg"].append(row["serial_msg"])

    if last_row is None:
        return

    json_data["serials"] = last_row["camera_serials"]
    json_data["exposure_times"] = last_row["exposure_times"]
    json_data["frame_rates_requested"] = last_row["frame_rates_requested"]
    json_data["frame_rates_binning"] = last_row["frame_rates_binning"]

    with open(tmp_file, "w") as handle:
        json.dump(json_data, handle)
        handle.write("\n")
    # Atomic replace to avoid exposing partially-written legacy JSON.
    import os

    os.replace(tmp_file, json_file)

    records_queue.put(
        SegmentRecord(
            filename=base_filename,
            timestamp_spread=0.0,
            recording_timestamp=recording_timestamp,
        ).as_dict()
    )


def metadata_finalize_queue(
    finalize_jobs_db: str,
    records_queue: Queue,
    stop_event: threading.Event,
):
    """Consume durable SQLite finalize jobs and build legacy JSON outputs."""
    repo = FinalizeJobsRepo(finalize_jobs_db)
    conn = repo.connect()
    try:
        # reset_in_progress_jobs is called once by the main thread before
        # workers are spawned (see recorder_service.start_workers).
        while True:
            if stop_event.is_set() and repo.count_pending(conn) == 0:
                break

            job = repo.claim_next_job(conn)
            if job is None:
                time.sleep(0.2)
                continue

            try:
                parsed_ts = datetime.fromisoformat(job.recording_timestamp)
                config_metadata = json.loads(job.config_metadata_json)
                finalize_legacy_json(
                    base_filename=job.base_filename,
                    config_metadata=config_metadata,
                    recording_timestamp=parsed_ts,
                    records_queue=records_queue,
                )
                repo.mark_done(conn, job.job_id)
            except Exception as exc:
                err = str(exc)
                tqdm.write(f"metadata finalize failure (job_id={job.job_id}): {err}")
                repo.mark_failed(conn, job.job_id, err)
    finally:
        conn.close()


def write_metadata_queue(
    json_queue: Queue,
    finalize_jobs_db: str,
    json_file: str,
    config_metadata: dict,
    worker_error_state: dict | None = None,
    stop_event: threading.Event | None = None,
    flush_done_event: threading.Event | None = None,
):
    """Write metadata queue to journal and enqueue segment finalization jobs."""
    current_filename = None
    current_first_local_time = None
    out = None
    repo = FinalizeJobsRepo(finalize_jobs_db)

    try:
        while True:
            try:
                frame = json_queue.get(timeout=1.0)
            except queue.Empty:
                if stop_event is not None and stop_event.is_set():
                    break
                continue

            try:
                if frame is None:
                    break

                base_filename = frame["base_filename"] if frame["base_filename"] is not None else json_file
                if current_filename != base_filename:
                    if out is not None:
                        out.close()
                        repo.enqueue_job(
                            base_filename=current_filename,
                            recording_timestamp=current_first_local_time,
                            config_metadata=config_metadata,
                        )

                    current_filename = base_filename
                    out = open(current_filename + ".metadata.jsonl", "a", buffering=1)
                    current_first_local_time = frame["local_times"]

                record = build_metadata_journal_record(frame)
                out.write(json.dumps(record))
                out.write("\n")
            except OSError as exc:
                msg = f"metadata journal I/O failure: {exc}"
                tqdm.write(msg)
                set_worker_error(worker_error_state, msg)
                break
            except Exception as exc:
                tqdm.write(f"metadata journal write skipped: {exc}")
                continue
            finally:
                json_queue.task_done()
    finally:
        if out is not None:
            out.close()
            repo.enqueue_job(
                base_filename=current_filename,
                recording_timestamp=current_first_local_time,
                config_metadata=config_metadata,
            )
        if flush_done_event is not None:
            flush_done_event.set()
