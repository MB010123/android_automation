"""Per-slot activity events (no SMS bodies)."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class SlotEvent:
    farm_slot_id: int
    event_type: str
    detail: str
    created_at: float


class SlotEventStore:
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
        if "slot_event_schema_version" not in tables:
            self._conn.execute(
                "CREATE TABLE slot_event_schema_version (version INTEGER NOT NULL)"
            )
            self._conn.execute(
                "INSERT INTO slot_event_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE slot_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    farm_slot_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX idx_slot_events_slot_time "
                "ON slot_events (farm_slot_id, created_at DESC)"
            )
            self._conn.commit()

    def append(self, farm_slot_id: int, event_type: str, detail: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO slot_events (farm_slot_id, event_type, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (farm_slot_id, event_type, detail[:500], now),
            )
            self._conn.commit()

    def list_events(
        self,
        farm_slot_id: int,
        *,
        since: float | None = None,
        limit: int = 50,
    ) -> list[SlotEvent]:
        limit = max(1, min(int(limit), 100))
        query = "SELECT * FROM slot_events WHERE farm_slot_id = ?"
        params: list[object] = [farm_slot_id]
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            SlotEvent(
                farm_slot_id=int(r["farm_slot_id"]),
                event_type=r["event_type"],
                detail=r["detail"],
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()
