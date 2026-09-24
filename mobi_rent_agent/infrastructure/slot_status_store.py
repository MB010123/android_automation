"""Per-bay heartbeat snapshot written by the VPS Farm heartbeat poller.

Only facts the Farm Agent actually reports are stored (ADB reachability).
Cellular/IMEI state is NOT stored here because the Farm health endpoint
does not expose it; callers must report those as unknown.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class SlotHeartbeat:
    farm_slot_id: int
    adb_online: bool
    last_checked_at: float
    last_seen_at: float | None
    farm_ok: bool
    farm_error: str | None


class SlotStatusStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "slot_status_schema_version" not in tables:
            self._conn.execute(
                "CREATE TABLE slot_status_schema_version (version INTEGER NOT NULL)"
            )
            self._conn.execute(
                "INSERT INTO slot_status_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        if "slot_heartbeats" not in tables:
            self._conn.execute(
                """
                CREATE TABLE slot_heartbeats (
                    farm_slot_id INTEGER PRIMARY KEY,
                    adb_online INTEGER NOT NULL,
                    last_checked_at REAL NOT NULL,
                    last_seen_at REAL,
                    farm_ok INTEGER NOT NULL,
                    farm_error TEXT
                )
                """
            )
        self._conn.commit()

    def record(
        self,
        farm_slot_id: int,
        *,
        adb_online: bool,
        farm_ok: bool,
        farm_error: str | None = None,
        now: float | None = None,
    ) -> SlotHeartbeat:
        ts = now if now is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seen_at FROM slot_heartbeats WHERE farm_slot_id = ?",
                (farm_slot_id,),
            ).fetchone()
            previous_seen = float(row["last_seen_at"]) if row and row["last_seen_at"] is not None else None
            last_seen = ts if adb_online else previous_seen
            self._conn.execute(
                """
                INSERT INTO slot_heartbeats
                    (farm_slot_id, adb_online, last_checked_at, last_seen_at, farm_ok, farm_error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(farm_slot_id) DO UPDATE SET
                    adb_online = excluded.adb_online,
                    last_checked_at = excluded.last_checked_at,
                    last_seen_at = excluded.last_seen_at,
                    farm_ok = excluded.farm_ok,
                    farm_error = excluded.farm_error
                """,
                (farm_slot_id, 1 if adb_online else 0, ts, last_seen, 1 if farm_ok else 0, farm_error),
            )
            self._conn.commit()
        return SlotHeartbeat(
            farm_slot_id=farm_slot_id,
            adb_online=adb_online,
            last_checked_at=ts,
            last_seen_at=last_seen,
            farm_ok=farm_ok,
            farm_error=farm_error,
        )

    def get(self, farm_slot_id: int) -> SlotHeartbeat | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM slot_heartbeats WHERE farm_slot_id = ?",
                (farm_slot_id,),
            ).fetchone()
        if row is None:
            return None
        return SlotHeartbeat(
            farm_slot_id=int(row["farm_slot_id"]),
            adb_online=bool(row["adb_online"]),
            last_checked_at=float(row["last_checked_at"]),
            last_seen_at=float(row["last_seen_at"]) if row["last_seen_at"] is not None else None,
            farm_ok=bool(row["farm_ok"]),
            farm_error=row["farm_error"],
        )

    def close(self) -> None:
        self._conn.close()
