"""Durable CLEANUP REQUIRED occupancy for a physical bay.

A bay in this table is never advertised as available and must not be
assigned until an administrator verifies the device and clears the flag.
No factory reset and no silent eSIM delete are performed here.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DeviceCleanupHold:
    farm_slot_id: int
    rental_id: str
    created_at: float
    detail: str


class DeviceCleanupStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    @property
    def db_path(self) -> Path:
        return self._path

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_cleanup_schema_version (
                version INTEGER NOT NULL
            )
            """
        )
        row = self._conn.execute("SELECT version FROM device_cleanup_schema_version").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO device_cleanup_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_cleanup_holds (
                    farm_slot_id INTEGER PRIMARY KEY,
                    rental_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    detail TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def is_required(self, farm_slot_id: int) -> bool:
        return self.get(farm_slot_id) is not None

    def get(self, farm_slot_id: int) -> DeviceCleanupHold | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT farm_slot_id, rental_id, created_at, detail "
                "FROM device_cleanup_holds WHERE farm_slot_id = ?",
                (int(farm_slot_id),),
            ).fetchone()
        if row is None:
            return None
        return DeviceCleanupHold(int(row[0]), str(row[1]), float(row[2]), str(row[3]))

    def mark_required(
        self,
        farm_slot_id: int,
        rental_id: str,
        *,
        detail: str,
        now: float | None = None,
    ) -> None:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO device_cleanup_holds
                    (farm_slot_id, rental_id, created_at, detail)
                VALUES (?, ?, ?, ?)
                """,
                (int(farm_slot_id), str(rental_id), stamp, str(detail)),
            )
            self._conn.commit()

    def clear(self, farm_slot_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM device_cleanup_holds WHERE farm_slot_id = ?",
                (int(farm_slot_id),),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def close(self) -> None:
        self._conn.close()
