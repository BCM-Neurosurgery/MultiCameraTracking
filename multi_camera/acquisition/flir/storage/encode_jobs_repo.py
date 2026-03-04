"""SQLite repository for durable journal->MP4 encode job tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import sqlite3


def get_encode_jobs_db_path(base_dir: str) -> str:
    return os.path.join(base_dir, "encode_jobs.sqlite3")


@dataclass(frozen=True)
class EncodeJob:
    job_id: int
    segment_base: str
    camera_serial: str
    journal_path: str
    output_mp4: str
    width: int
    height: int
    fps: float
    bayer_pattern: str
    frame_count: int


class EncodeJobsRepo:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS encode_jobs_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    segment_base TEXT NOT NULL,
                    camera_serial TEXT NOT NULL,
                    journal_path TEXT NOT NULL,
                    output_mp4 TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    fps REAL NOT NULL,
                    bayer_pattern TEXT NOT NULL,
                    frame_count INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retries INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    worker_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_encode_jobs_v2_status_created
                ON encode_jobs_v2(status, created_at)
                """
            )
            conn.commit()

    def enqueue_job(
        self,
        segment_base: str,
        camera_serial: str,
        journal_path: str,
        output_mp4: str,
        width: int,
        height: int,
        fps: float,
        bayer_pattern: str,
        frame_count: int,
    ):
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                INSERT INTO encode_jobs_v2 (
                    segment_base, camera_serial, journal_path, output_mp4,
                    width, height, fps, bayer_pattern, frame_count,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (segment_base, camera_serial, journal_path, output_mp4, width, height, float(fps), bayer_pattern, frame_count, now, now),
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
            UPDATE encode_jobs_v2
            SET status='pending', updated_at=?
            WHERE status='in_progress'
            """,
            (datetime.utcnow().isoformat(),),
        )
        conn.commit()

    @staticmethod
    def claim_next_job(conn: sqlite3.Connection, worker_id: str) -> EncodeJob | None:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, segment_base, camera_serial, journal_path, output_mp4,
                   width, height, fps, bayer_pattern, frame_count
            FROM encode_jobs_v2
            WHERE status='pending'
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        now = datetime.utcnow().isoformat()
        conn.execute(
            """
            UPDATE encode_jobs_v2
            SET status='in_progress', worker_id=?, updated_at=?
            WHERE id=?
            """,
            (worker_id, now, row[0]),
        )
        conn.execute("COMMIT")
        return EncodeJob(
            job_id=int(row[0]),
            segment_base=row[1],
            camera_serial=row[2],
            journal_path=row[3],
            output_mp4=row[4],
            width=int(row[5]),
            height=int(row[6]),
            fps=float(row[7]),
            bayer_pattern=row[8],
            frame_count=int(row[9]),
        )

    @staticmethod
    def mark_done(conn: sqlite3.Connection, job_id: int):
        conn.execute(
            """
            UPDATE encode_jobs_v2
            SET status='done', updated_at=?
            WHERE id=?
            """,
            (datetime.utcnow().isoformat(), job_id),
        )
        conn.commit()

    @staticmethod
    def mark_failed(conn: sqlite3.Connection, job_id: int, err: str):
        conn.execute(
            """
            UPDATE encode_jobs_v2
            SET status='failed', retries=retries+1, last_error=?, updated_at=?
            WHERE id=?
            """,
            (err, datetime.utcnow().isoformat(), job_id),
        )
        conn.commit()

    @staticmethod
    def count_pending(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM encode_jobs_v2
            WHERE status IN ('pending', 'in_progress')
            """
        ).fetchone()
        return int(row[0]) if row is not None else 0
