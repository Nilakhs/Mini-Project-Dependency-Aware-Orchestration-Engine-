"""
Telemetry Database Persistence

Stores build history, structured telemetry events, and benchmark comparisons in SQLite.
Uses WAL mode for concurrent, crash-resilient access alongside the build cache.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

from telemetry.events import TelemetryEvent


class TelemetryDB:
    """
    SQLite persistence layer for build telemetry and benchmark analytics.
    """

    def __init__(self, db_path: str = "data/build_state.db"):
        self.db_path = db_path
        self.is_memory = (db_path == ":memory:")
        
        if not self.is_memory:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Initialize tables and indexes with WAL mode."""
        cur = self._conn.cursor()
        if not self.is_memory:
            cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA foreign_keys=ON;")

        cur.executescript("""
        CREATE TABLE IF NOT EXISTS telemetry_builds (
            build_id TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            status TEXT NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            duration REAL NOT NULL,
            max_workers INTEGER NOT NULL,
            peak_concurrency INTEGER NOT NULL,
            total_tasks INTEGER NOT NULL,
            completed_count INTEGER NOT NULL,
            skipped_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL,
            blocked_count INTEGER NOT NULL,
            cpu_time REAL NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS telemetry_events (
            event_id TEXT PRIMARY KEY,
            build_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            timestamp REAL NOT NULL,
            iso_time TEXT NOT NULL,
            task_id TEXT,
            task_name TEXT,
            worker_id INTEGER,
            status TEXT,
            duration REAL,
            message TEXT,
            stdout TEXT,
            stderr TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_telemetry_events_build_id ON telemetry_events(build_id);
        CREATE INDEX IF NOT EXISTS idx_telemetry_events_timestamp ON telemetry_events(timestamp);

        CREATE TABLE IF NOT EXISTS benchmark_comparisons (
            comparison_id TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            sequential_duration REAL NOT NULL,
            parallel_duration REAL NOT NULL,
            worker_count INTEGER NOT NULL,
            speedup REAL NOT NULL,
            efficiency REAL NOT NULL,
            time_saved REAL NOT NULL,
            pct_reduction REAL NOT NULL,
            timestamp REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        self._conn.commit()

    def record_event(self, event: TelemetryEvent) -> None:
        """Persist a single telemetry event."""
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO telemetry_events (
                    event_id, build_id, event_type, timestamp, iso_time,
                    task_id, task_name, worker_id, status, duration,
                    message, stdout, stderr
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.build_id,
                    event.event_type.value,
                    event.timestamp,
                    event.iso_time,
                    event.task_id,
                    event.task_name,
                    event.worker_id,
                    event.status,
                    event.duration,
                    event.message,
                    event.stdout,
                    event.stderr,
                ),
            )

    def record_build(self, data: dict[str, Any]) -> None:
        """Persist a completed build summary."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO telemetry_builds (
                    build_id, target, status, start_time, end_time, duration,
                    max_workers, peak_concurrency, total_tasks,
                    completed_count, skipped_count, failed_count, blocked_count,
                    cpu_time, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["build_id"],
                    data["target"],
                    data["status"],
                    data["start_time"],
                    data["end_time"],
                    data["duration"],
                    data["max_workers"],
                    data["peak_concurrency"],
                    data["total_tasks"],
                    data["completed_count"],
                    data["skipped_count"],
                    data["failed_count"],
                    data["blocked_count"],
                    data.get("cpu_time", 0.0),
                    data.get("created_at", now_iso),
                ),
            )

    def record_comparison(self, comp: dict[str, Any]) -> None:
        """Persist a sequential vs parallel benchmark comparison."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO benchmark_comparisons (
                    comparison_id, target, sequential_duration, parallel_duration,
                    worker_count, speedup, efficiency, time_saved, pct_reduction,
                    timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comp["comparison_id"],
                    comp["target"],
                    comp["sequential_duration"],
                    comp["parallel_duration"],
                    comp["worker_count"],
                    comp["speedup"],
                    comp["efficiency"],
                    comp["time_saved"],
                    comp["pct_reduction"],
                    comp.get("timestamp", time.time()),
                    comp.get("created_at", now_iso),
                ),
            )

    def get_builds(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return historical builds ordered by newest first."""
        cur = self._conn.execute(
            """
            SELECT * FROM telemetry_builds
            ORDER BY start_time DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_build(self, build_id: str) -> Optional[dict[str, Any]]:
        """Get summary record for a specific build ID."""
        cur = self._conn.execute(
            "SELECT * FROM telemetry_builds WHERE build_id = ?",
            (build_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_build_events(self, build_id: str) -> list[dict[str, Any]]:
        """Return all events for a build ordered by timestamp."""
        cur = self._conn.execute(
            """
            SELECT * FROM telemetry_events
            WHERE build_id = ?
            ORDER BY timestamp ASC, event_id ASC
            """,
            (build_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_comparisons(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return benchmark comparisons ordered by newest first."""
        cur = self._conn.execute(
            """
            SELECT * FROM benchmark_comparisons
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_latest_comparison(self) -> Optional[dict[str, Any]]:
        """Return the most recent benchmark comparison record."""
        cur = self._conn.execute(
            "SELECT * FROM benchmark_comparisons ORDER BY timestamp DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        """Close SQLite connection."""
        try:
            self._conn.close()
        except Exception:
            pass
