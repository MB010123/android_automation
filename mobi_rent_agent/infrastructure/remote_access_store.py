"""Remote-access session state per rental (SQLite, VPS side).

Stores which rental currently holds a platform lease on which slot/device
and when it expires. The platform password is *not* persisted: it is shown
to the customer exactly once in the create response.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from domain.remote_access import public_activation_view

SCHEMA_VERSION = 2

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_RELEASED = "released"
STATUS_EXPIRED = "expired"


@dataclass(frozen=True)
class RemoteAccessSession:
    rental_id: str
    customer_id: str
    slot_id: int
    device_id: str
    platform_username: str
    status: str
    created_at: float
    expires_at: float
    ended_at: float | None = None
    prepare_state: str | None = None
    prepare_detail: str | None = None
    prepare_job_id: str | None = None
    activation_observed: str | None = None

    def is_active(self, now: float) -> bool:
        return self.status == STATUS_ACTIVE and now < self.expires_at

    def to_public_dict(self, now: float) -> dict:
        """Browser-safe view. Never includes platform credentials or device serial."""
        active = self.is_active(now)
        status = self.status
        if status == STATUS_ACTIVE and not active:
            status = STATUS_EXPIRED
        body = {
            "rental_id": self.rental_id,
            "slot_id": self.slot_id,
            "status": status,
            "active": active,
            "expires_at": self.expires_at,
            "prepare_state": self.prepare_state,
            "prepare_detail": self.prepare_detail,
        }
        body.update(
            public_activation_view(
                prepare_state=self.prepare_state,
                activation_observed=self.activation_observed,
            )
        )
        return body


class RemoteAccessSessionStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        tables = {
            row[0]
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "remote_access_schema_version" not in tables:
            self._conn.execute("CREATE TABLE remote_access_schema_version (version INTEGER NOT NULL)")
            self._conn.execute(
                "INSERT INTO remote_access_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE remote_access_sessions (
                    rental_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    device_id TEXT NOT NULL,
                    platform_username TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    ended_at REAL,
                    prepare_state TEXT,
                    prepare_detail TEXT,
                    prepare_job_id TEXT,
                    activation_observed TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX remote_access_sessions_slot ON remote_access_sessions (slot_id, status)"
            )
            self._conn.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        row = self._conn.execute("SELECT version FROM remote_access_schema_version").fetchone()
        version = int(row[0]) if row else 1
        if version < 2:
            columns = {
                info[1]
                for info in self._conn.execute("PRAGMA table_info(remote_access_sessions)").fetchall()
            }
            if "activation_observed" not in columns:
                self._conn.execute("ALTER TABLE remote_access_sessions ADD COLUMN activation_observed TEXT")
            self._conn.execute("UPDATE remote_access_schema_version SET version = ?", (SCHEMA_VERSION,))
            self._conn.commit()

    _COLUMNS = (
        "rental_id, customer_id, slot_id, device_id, platform_username, status, "
        "created_at, expires_at, ended_at, prepare_state, prepare_detail, prepare_job_id, "
        "activation_observed"
    )

    @staticmethod
    def _row_to_session(row) -> RemoteAccessSession:
        return RemoteAccessSession(
            rental_id=str(row[0]),
            customer_id=str(row[1]),
            slot_id=int(row[2]),
            device_id=str(row[3]),
            platform_username=str(row[4]),
            status=str(row[5]),
            created_at=float(row[6]),
            expires_at=float(row[7]),
            ended_at=float(row[8]) if row[8] is not None else None,
            prepare_state=row[9],
            prepare_detail=row[10],
            prepare_job_id=row[11],
            activation_observed=row[12] if len(row) > 12 else None,
        )

    def get(self, rental_id: str) -> RemoteAccessSession | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM remote_access_sessions WHERE rental_id = ?",
                (str(rental_id),),
            ).fetchone()
        return self._row_to_session(row) if row else None

    def active_for_slot(self, slot_id: int, *, now: float | None = None) -> RemoteAccessSession | None:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT {self._COLUMNS} FROM remote_access_sessions
                WHERE slot_id = ? AND status = ? AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (int(slot_id), STATUS_ACTIVE, now),
            ).fetchone()
        return self._row_to_session(row) if row else None

    def list_active(self, *, now: float | None = None) -> list[RemoteAccessSession]:
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT {self._COLUMNS} FROM remote_access_sessions
                WHERE status = ? AND expires_at > ?
                """,
                (STATUS_ACTIVE, now),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def upsert(self, session: RemoteAccessSession) -> None:
        with self._lock:
            self._conn.execute(
                f"""
                INSERT OR REPLACE INTO remote_access_sessions ({self._COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.rental_id,
                    session.customer_id,
                    int(session.slot_id),
                    session.device_id,
                    session.platform_username,
                    session.status,
                    float(session.created_at),
                    float(session.expires_at),
                    session.ended_at,
                    session.prepare_state,
                    session.prepare_detail,
                    session.prepare_job_id,
                    session.activation_observed,
                ),
            )
            self._conn.commit()

    def end(self, rental_id: str, *, status: str, now: float | None = None) -> RemoteAccessSession | None:
        current = self.get(rental_id)
        if current is None:
            return None
        ended = replace(current, status=status, ended_at=time.time() if now is None else now)
        self.upsert(ended)
        return ended

    def set_prepare_state(
        self,
        rental_id: str,
        state: str,
        *,
        detail: str | None = None,
        job_id: str | None = None,
    ) -> None:
        current = self.get(rental_id)
        if current is None:
            return
        self.upsert(
            replace(
                current,
                prepare_state=state,
                prepare_detail=detail,
                prepare_job_id=job_id if job_id is not None else current.prepare_job_id,
            )
        )

    def set_activation_observed(self, rental_id: str, observed: str, *, detail: str | None = None) -> None:
        current = self.get(rental_id)
        if current is None:
            return
        self.upsert(replace(current, activation_observed=observed, prepare_detail=detail if detail is not None else current.prepare_detail))

    def close(self) -> None:
        self._conn.close()
