"""Durable rental occupancy for a physical bay.

Keyed by farm_slot_id. Survives assign/provision job completion and VPS
process restart. Released only by explicit rental-end/release, never by
the job worker.

Tenant/Supabase `user_id` remains customer ownership. This table is the
VPS mutex so two rentals cannot occupy the same bay.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SlotReservation:
    farm_slot_id: int
    rental_id: str
    created_at: float


class SlotReservationStore:
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
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "slot_reservation_schema_version" not in tables:
            self._conn.execute(
                "CREATE TABLE slot_reservation_schema_version (version INTEGER NOT NULL)"
            )
            self._conn.execute(
                "INSERT INTO slot_reservation_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE slot_reservations (
                    farm_slot_id INTEGER PRIMARY KEY,
                    rental_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX idx_slot_reservations_rental "
                "ON slot_reservations (rental_id)"
            )
            self._conn.commit()
            return
        self._migrate_schema()

    def _schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT version FROM slot_reservation_schema_version"
        ).fetchone()
        return int(row[0]) if row else 0

    def _migrate_schema(self) -> None:
        version = self._schema_version()
        if version >= SCHEMA_VERSION:
            return
        self._conn.execute(
            "UPDATE slot_reservation_schema_version SET version = ?",
            (SCHEMA_VERSION,),
        )
        self._conn.commit()

    def is_reserved(self, farm_slot_id: int) -> bool:
        return self.get(farm_slot_id) is not None

    def get(self, farm_slot_id: int) -> SlotReservation | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT farm_slot_id, rental_id, created_at
                FROM slot_reservations WHERE farm_slot_id = ?
                """,
                (int(farm_slot_id),),
            ).fetchone()
        if row is None:
            return None
        return SlotReservation(
            farm_slot_id=int(row[0]),
            rental_id=str(row[1]),
            created_at=float(row[2]),
        )

    def get_by_rental(self, rental_id: str) -> SlotReservation | None:
        text = str(rental_id or "").strip()
        if not text:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT farm_slot_id, rental_id, created_at
                FROM slot_reservations WHERE rental_id = ?
                LIMIT 1
                """,
                (text,),
            ).fetchone()
        if row is None:
            return None
        return SlotReservation(
            farm_slot_id=int(row[0]),
            rental_id=str(row[1]),
            created_at=float(row[2]),
        )

    def claim(self, farm_slot_id: int, rental_id: str) -> bool:
        """Hold `farm_slot_id` for `rental_id`. Idempotent for the same rental."""
        bay = int(farm_slot_id)
        rental = str(rental_id or "").strip()
        if not rental:
            return False
        now = time.time()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO slot_reservations (farm_slot_id, rental_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (bay, rental, now),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                row = self._conn.execute(
                    "SELECT rental_id FROM slot_reservations WHERE farm_slot_id = ?",
                    (bay,),
                ).fetchone()
        return bool(row) and str(row[0]) == rental

    def list_all(self) -> list[SlotReservation]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT farm_slot_id, rental_id, created_at
                FROM slot_reservations
                ORDER BY farm_slot_id
                """
            ).fetchall()
        return [
            SlotReservation(
                farm_slot_id=int(row[0]),
                rental_id=str(row[1]),
                created_at=float(row[2]),
            )
            for row in rows
        ]

    def release(self, farm_slot_id: int, rental_id: str) -> bool:
        """Drop this rental's hold only. Never deletes a different rental's row."""
        bay = int(farm_slot_id)
        rental = str(rental_id or "").strip()
        if not rental:
            return False
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM slot_reservations WHERE farm_slot_id = ? AND rental_id = ?",
                (bay, rental),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
