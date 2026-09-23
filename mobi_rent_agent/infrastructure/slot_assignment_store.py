"""Active bay assignment state (prevents double assign)."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class SlotAssignment:
    farm_slot_id: int
    rental_id: str
    job_id: str
    created_at: float


class SlotAssignmentStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "slot_assignment_schema_version" not in tables:
            self._conn.execute(
                "CREATE TABLE slot_assignment_schema_version (version INTEGER NOT NULL)"
            )
            self._conn.execute(
                "INSERT INTO slot_assignment_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE slot_assignments (
                    farm_slot_id INTEGER PRIMARY KEY,
                    rental_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.commit()

    def is_assigned(self, farm_slot_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM slot_assignments WHERE farm_slot_id = ?",
                (farm_slot_id,),
            ).fetchone()
        return row is not None

    def claim(self, farm_slot_id: int, rental_id: str, job_id: str) -> bool:
        now = time.time()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO slot_assignments (farm_slot_id, rental_id, job_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (farm_slot_id, rental_id, job_id, now),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False

    def release(self, farm_slot_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM slot_assignments WHERE farm_slot_id = ?",
                (farm_slot_id,),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
