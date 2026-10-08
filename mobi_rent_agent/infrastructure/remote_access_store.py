"""Remote-access session state per rental (SQLite, VPS side).

Stores which rental currently holds a platform lease on which slot/device
and when it expires. The platform password is persisted only on the VPS so
the in-app stream/control proxy can authenticate to GADS. It is never
returned in public session views.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.remote_access import public_activation_view

SCHEMA_VERSION = 4

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_RELEASED = "released"
STATUS_EXPIRED = "expired"

PREPARE_IN_PROGRESS = frozenset({"placing_qr", "rebooting", "waiting_adb", "waiting_platform"})


def _iso_utc(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_evidence(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    allowed = (
        "verdict",
        "esim_profile_present",
        "esim_enabled",
        "network_registered",
        "cellular",
        "observation_complete",
    )
    body = {key: raw[key] for key in allowed if key in raw and raw[key] is not None}
    return body or None


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
    activation_observed_at: float | None = None
    activation_evidence: dict[str, Any] | None = None
    platform_secret: str | None = None
    setup_phase: str | None = None
    setup_complete: bool = False
    voidfix_observed: str | None = None

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
            "activation_observed": self.activation_observed,
            "activation_observed_at": _iso_utc(self.activation_observed_at),
            "session_mode": "in_app",
            "setup_phase": self.setup_phase or "esim",
            "setup_complete": bool(self.setup_complete),
            "voidfix_observed": self.voidfix_observed,
            "stream_path": f"/rentals/{self.rental_id}/remote-access/stream",
            "allowed_controls": ["tap", "swipe", "type", "back", "home", "recents"],
            "coordinate_space": "native_device_pixels",
        }
        body.update(
            public_activation_view(
                prepare_state=self.prepare_state,
                activation_observed=self.activation_observed,
            )
        )
        if self.activation_evidence:
            body["activation_evidence"] = dict(self.activation_evidence)
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
                    activation_observed TEXT,
                    activation_observed_at REAL,
                    activation_evidence TEXT,
                    platform_secret TEXT,
                    setup_phase TEXT,
                    setup_complete INTEGER,
                    voidfix_observed TEXT
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
        columns = {
            info[1]
            for info in self._conn.execute("PRAGMA table_info(remote_access_sessions)").fetchall()
        }
        if version < 2 or "activation_observed" not in columns:
            if "activation_observed" not in columns:
                self._conn.execute("ALTER TABLE remote_access_sessions ADD COLUMN activation_observed TEXT")
        if version < 3 or "activation_observed_at" not in columns:
            if "activation_observed_at" not in columns:
                self._conn.execute("ALTER TABLE remote_access_sessions ADD COLUMN activation_observed_at REAL")
            if "activation_evidence" not in columns:
                self._conn.execute("ALTER TABLE remote_access_sessions ADD COLUMN activation_evidence TEXT")
        if version < 4:
            if "platform_secret" not in columns:
                self._conn.execute("ALTER TABLE remote_access_sessions ADD COLUMN platform_secret TEXT")
            if "setup_phase" not in columns:
                self._conn.execute("ALTER TABLE remote_access_sessions ADD COLUMN setup_phase TEXT")
            if "setup_complete" not in columns:
                self._conn.execute("ALTER TABLE remote_access_sessions ADD COLUMN setup_complete INTEGER")
            if "voidfix_observed" not in columns:
                self._conn.execute("ALTER TABLE remote_access_sessions ADD COLUMN voidfix_observed TEXT")
        self._conn.execute("UPDATE remote_access_schema_version SET version = ?", (SCHEMA_VERSION,))
        self._conn.commit()

    _COLUMNS = (
        "rental_id, customer_id, slot_id, device_id, platform_username, status, "
        "created_at, expires_at, ended_at, prepare_state, prepare_detail, prepare_job_id, "
        "activation_observed, activation_observed_at, activation_evidence, "
        "platform_secret, setup_phase, setup_complete, voidfix_observed"
    )

    @staticmethod
    def _row_to_session(row) -> RemoteAccessSession:
        evidence = None
        if len(row) > 14 and row[14]:
            try:
                parsed = json.loads(str(row[14]))
            except (TypeError, ValueError):
                parsed = None
            evidence = _safe_evidence(parsed)
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
            activation_observed_at=float(row[13]) if len(row) > 13 and row[13] is not None else None,
            activation_evidence=evidence,
            platform_secret=str(row[15]) if len(row) > 15 and row[15] else None,
            setup_phase=str(row[16]) if len(row) > 16 and row[16] else None,
            setup_complete=bool(row[17]) if len(row) > 17 and row[17] else False,
            voidfix_observed=str(row[18]) if len(row) > 18 and row[18] else None,
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
        evidence = _safe_evidence(session.activation_evidence)
        with self._lock:
            self._conn.execute(
                f"""
                INSERT OR REPLACE INTO remote_access_sessions ({self._COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    session.activation_observed_at,
                    json.dumps(evidence) if evidence else None,
                    session.platform_secret,
                    session.setup_phase,
                    1 if session.setup_complete else 0,
                    session.voidfix_observed,
                ),
            )
            self._conn.commit()

    def end(self, rental_id: str, *, status: str, now: float | None = None) -> RemoteAccessSession | None:
        current = self.get(rental_id)
        if current is None:
            return None
        ended = replace(
            current,
            status=status,
            ended_at=time.time() if now is None else now,
            platform_secret=None,
        )
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

    def set_activation_observed(
        self,
        rental_id: str,
        observed: str,
        *,
        observed_at: float | None = None,
        evidence: dict[str, Any] | None = None,
        detail: str | None = None,
    ) -> None:
        current = self.get(rental_id)
        if current is None:
            return
        self.upsert(
            replace(
                current,
                activation_observed=observed,
                activation_observed_at=observed_at if observed_at is not None else time.time(),
                activation_evidence=_safe_evidence(evidence),
                prepare_detail=detail if detail is not None else current.prepare_detail,
            )
        )

    def reconcile_interrupted_prepares(self) -> int:
        """After VPS restart, in-flight prepare/reboot cannot be trusted. Mark failed."""
        count = 0
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM remote_access_sessions WHERE prepare_state IN (?, ?, ?, ?)",
                tuple(sorted(PREPARE_IN_PROGRESS)),
            ).fetchall()
        for row in rows:
            session = self._row_to_session(row)
            self.upsert(
                replace(
                    session,
                    prepare_state="failed",
                    prepare_detail="interrupted_by_restart",
                )
            )
            count += 1
        return count

    def close(self) -> None:
        self._conn.close()
