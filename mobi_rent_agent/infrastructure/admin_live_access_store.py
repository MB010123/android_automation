"""Short-lived admin live-view sessions keyed by session id (not rental id).

Does not store GADS passwords, Farm tokens, ADB serials in public views,
rental ids, or cleanup state. Closing a row only ends this admin session.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from application.in_app_control_policy import public_allowed_controls

SCHEMA_VERSION = 1
STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"
STATUS_EXPIRED = "expired"


def _iso_utc(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class AdminLiveAccessSession:
    session_id: str
    admin_user_id: str
    slot_id: int
    device_id: str
    status: str
    created_at: float
    expires_at: float
    ended_at: float | None = None

    def is_active(self, now: float) -> bool:
        return self.status == STATUS_ACTIVE and now < self.expires_at

    def to_public_dict(self, now: float) -> dict:
        """Browser-safe view. Never includes serials, tokens, or GADS URLs."""
        active = self.is_active(now)
        status = self.status
        if status == STATUS_ACTIVE and not active:
            status = STATUS_EXPIRED
        return {
            "ok": True,
            "session_id": self.session_id,
            "slot_id": self.slot_id,
            "status": status,
            "active": active,
            "expires_at": self.expires_at,
            "expires_at_iso": _iso_utc(self.expires_at),
            "stream_path": (
                f"/admin/devices/{self.slot_id}/live-access/stream"
                f"?session={self.session_id}"
            ),
            "allowed_controls": public_allowed_controls(
                setup_mode=False, session_restrictions=False
            ),
            "coordinate_space": "native_device_pixels",
            "session_mode": "admin_live_access",
            "rental_unchanged": True,
            "cleanup_unchanged": True,
        }


class AdminLiveAccessStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    @property
    def db_path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_live_access_schema_version (
                version INTEGER NOT NULL
            )
            """
        )
        row = self._conn.execute(
            "SELECT version FROM admin_live_access_schema_version"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO admin_live_access_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_live_access_sessions (
                    session_id TEXT PRIMARY KEY,
                    admin_user_id TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    device_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    ended_at REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS admin_live_access_slot_admin "
                "ON admin_live_access_sessions (slot_id, admin_user_id, status)"
            )
            self._conn.commit()

    _COLUMNS = (
        "session_id, admin_user_id, slot_id, device_id, status, "
        "created_at, expires_at, ended_at"
    )

    @staticmethod
    def _row(row) -> AdminLiveAccessSession:
        return AdminLiveAccessSession(
            session_id=str(row[0]),
            admin_user_id=str(row[1]),
            slot_id=int(row[2]),
            device_id=str(row[3]),
            status=str(row[4]),
            created_at=float(row[5]),
            expires_at=float(row[6]),
            ended_at=float(row[7]) if row[7] is not None else None,
        )

    def get(self, session_id: str) -> AdminLiveAccessSession | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM admin_live_access_sessions "
                "WHERE session_id = ?",
                (sid,),
            ).fetchone()
        return self._row(row) if row else None

    def active_for_admin_slot(
        self, *, admin_user_id: str, slot_id: int, now: float | None = None
    ) -> AdminLiveAccessSession | None:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT {self._COLUMNS} FROM admin_live_access_sessions
                WHERE admin_user_id = ? AND slot_id = ? AND status = ? AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(admin_user_id), int(slot_id), STATUS_ACTIVE, now),
            ).fetchone()
        return self._row(row) if row else None

    def create(
        self,
        *,
        admin_user_id: str,
        slot_id: int,
        device_id: str,
        ttl_seconds: float,
        now: float | None = None,
        session_id: str | None = None,
    ) -> AdminLiveAccessSession:
        ts = time.time() if now is None else now
        session = AdminLiveAccessSession(
            session_id=session_id or str(uuid.uuid4()),
            admin_user_id=str(admin_user_id),
            slot_id=int(slot_id),
            device_id=str(device_id),
            status=STATUS_ACTIVE,
            created_at=ts,
            expires_at=ts + float(ttl_seconds),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO admin_live_access_sessions (
                    session_id, admin_user_id, slot_id, device_id, status,
                    created_at, expires_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    session.session_id,
                    session.admin_user_id,
                    session.slot_id,
                    session.device_id,
                    session.status,
                    session.created_at,
                    session.expires_at,
                ),
            )
            self._conn.commit()
        return session

    def close_session(self, session_id: str, *, now: float | None = None) -> AdminLiveAccessSession | None:
        ts = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                """
                UPDATE admin_live_access_sessions
                SET status = ?, ended_at = ?
                WHERE session_id = ? AND status = ?
                """,
                (STATUS_CLOSED, ts, str(session_id), STATUS_ACTIVE),
            )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM admin_live_access_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        return self._row(row) if row else None

    def expire_stale(self, *, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE admin_live_access_sessions
                SET status = ?, ended_at = ?
                WHERE status = ? AND expires_at <= ?
                """,
                (STATUS_EXPIRED, ts, STATUS_ACTIVE, ts),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)
