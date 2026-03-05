"""SQLite repository for durable metadata-finalization job tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
import sqlite3


def get_finalize_jobs_db_path(base_dir: str) -> str:
    return os.path.join(base_dir, "metadata_finalize_jobs.sqlite3")


@dataclass(frozen=True)
class FinalizeJob:
    job_id: int
    base_filename: str
    recording_timestamp: str
    config_metadata_json: str


class FinalizeJobsRepo:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata_finalize_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    base_filename TEXT NOT NULL,
                    recording_timestamp TEXT NOT NULL,
                    config_metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retries INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_metadata_finalize_jobs_status_created
                ON metadata_finalize_jobs(status, created_at)
                """)
            conn.commit()

    def enqueue_job(self, base_filename: str, recording_timestamp: datetime, config_metadata: dict):
        now = datetime.utcnow().isoformat()
        rec_ts = recording_timestamp.isoformat() if isinstance(recording_timestamp, datetime) else str(recording_timestamp)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                INSERT INTO metadata_finalize_jobs (
                    base_filename, recording_timestamp, config_metadata_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (base_filename, rec_ts, json.dumps(config_metadata), now, now),
            )
            conn.commit()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def reset_in_progress_jobs(conn: sqlite3.Connection):
        """
        Requeue jobs that were left in_progress after an unclean shutdown.
        """
        conn.execute(
            """
            UPDATE metadata_finalize_jobs
            SET status='pending', updated_at=?
            WHERE status='in_progress'
            """,
            (datetime.utcnow().isoformat(),),
        )
        conn.commit()

    @staticmethod
    def claim_next_job(conn: sqlite3.Connection) -> FinalizeJob | None:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""
                SELECT id, base_filename, recording_timestamp, config_metadata_json
                FROM metadata_finalize_jobs
                WHERE status='pending'
                ORDER BY created_at ASC
                LIMIT 1
                """).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None

            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                UPDATE metadata_finalize_jobs
                SET status='in_progress', updated_at=?
                WHERE id=?
                """,
                (now, row[0]),
            )
            conn.execute("COMMIT")
            return FinalizeJob(
                job_id=int(row[0]),
                base_filename=row[1],
                recording_timestamp=row[2],
                config_metadata_json=row[3],
            )
        except sqlite3.OperationalError:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return None

    @staticmethod
    def mark_done(conn: sqlite3.Connection, job_id: int):
        conn.execute(
            """
            UPDATE metadata_finalize_jobs
            SET status='done', updated_at=?
            WHERE id=?
            """,
            (datetime.utcnow().isoformat(), job_id),
        )
        conn.commit()

    MAX_RETRIES = 3

    @staticmethod
    def mark_failed(conn: sqlite3.Connection, job_id: int, err: str):
        conn.execute(
            """
            UPDATE metadata_finalize_jobs
            SET status = CASE WHEN retries < ? THEN 'pending' ELSE 'failed' END,
                retries = retries + 1,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (FinalizeJobsRepo.MAX_RETRIES, err, datetime.utcnow().isoformat(), job_id),
        )
        conn.commit()

    @staticmethod
    def count_pending(conn: sqlite3.Connection) -> int:
        row = conn.execute("""
            SELECT COUNT(*)
            FROM metadata_finalize_jobs
            WHERE status IN ('pending', 'in_progress')
            """).fetchone()
        return int(row[0]) if row is not None else 0
