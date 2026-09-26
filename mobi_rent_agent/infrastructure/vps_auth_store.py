"""SQLite persistence for VPS user auth, sessions, tokens, slot ownership, eSIM records."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class AuthUser:
    user_id: str
    email: str
    password_hash: str
    is_active: bool
    email_verified: bool
    role: str
    created_at: float
    updated_at: float
    last_login_at: float | None
    failed_login_attempts: int
    locked_until: float | None


@dataclass
class AuthSession:
    session_id: str
    user_id: str
    token_hash: str
    created_at: float
    expires_at: float
    revoked_at: float | None
    last_seen_at: float | None
    ip: str | None
    user_agent: str | None


@dataclass
class SlotOwnership:
    farm_slot_id: int
    user_id: str
    rental_id: str | None
    created_at: float
    updated_at: float


def _user_from_row(row: sqlite3.Row) -> AuthUser:
    return AuthUser(
        user_id=str(row["id"]),
        email=str(row["email"]),
        password_hash=str(row["password_hash"]),
        is_active=bool(row["is_active"]),
        email_verified=bool(row["email_verified"]),
        role=str(row["role"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        last_login_at=float(row["last_login_at"]) if row["last_login_at"] is not None else None,
        failed_login_attempts=int(row["failed_login_attempts"]),
        locked_until=float(row["locked_until"]) if row["locked_until"] is not None else None,
    )


class VpsAuthStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._init_schema()

    def foreign_keys_enabled(self) -> bool:
        with self._lock:
            row = self._conn.execute("PRAGMA foreign_keys").fetchone()
        return bool(row[0]) if row is not None else False

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_schema_version (
                    version INTEGER NOT NULL
                )
                """
            )
            row = self._conn.execute("SELECT version FROM auth_schema_version").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO auth_schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    email_verified INTEGER NOT NULL DEFAULT 0,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_login_at REAL,
                    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL,
                    last_seen_at REAL,
                    ip TEXT,
                    user_agent TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_verify_tokens (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS slot_ownership (
                    farm_slot_id INTEGER PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    rental_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS esim_uploads (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    farm_slot_id INTEGER NOT NULL,
                    storage_key TEXT NOT NULL,
                    rental_id TEXT,
                    carrier TEXT,
                    job_id TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            self._conn.commit()

    def create_user(self, *, email: str, password_hash: str, now: float | None = None) -> AuthUser | None:
        ts = now if now is not None else time.time()
        user_id = str(uuid.uuid4())
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO users (
                        id, email, password_hash, is_active, email_verified, role,
                        created_at, updated_at, last_login_at, failed_login_attempts, locked_until
                    ) VALUES (?, ?, ?, 1, 0, 'user', ?, ?, NULL, 0, NULL)
                    """,
                    (user_id, email, password_hash, ts, ts),
                )
                self._conn.execute(
                    "INSERT INTO profiles (user_id, email, created_at) VALUES (?, ?, ?)",
                    (user_id, email, ts),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return None
        user = self.get_user_by_id(user_id)
        return user

    def get_user_by_id(self, user_id: str) -> AuthUser | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_from_row(row) if row else None

    def get_user_by_email(self, email: str) -> AuthUser | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _user_from_row(row) if row else None

    def record_login_success(self, user_id: str, *, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE users SET last_login_at = ?, failed_login_attempts = 0,
                locked_until = NULL, updated_at = ? WHERE id = ?
                """,
                (ts, ts, user_id),
            )
            self._conn.commit()

    def record_login_failure(
        self,
        user_id: str,
        *,
        lock_after: int,
        lock_seconds: float,
        now: float | None = None,
    ) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT failed_login_attempts FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            attempts = int(row["failed_login_attempts"]) + 1 if row else 1
            locked_until = ts + lock_seconds if attempts >= lock_after else None
            self._conn.execute(
                """
                UPDATE users SET failed_login_attempts = ?, locked_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempts, locked_until, ts, user_id),
            )
            self._conn.commit()

    def set_password_hash(self, user_id: str, password_hash: str, *, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, ts, user_id),
            )
            self._conn.commit()

    def set_email_verified(self, user_id: str, *, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE users SET email_verified = 1, updated_at = ? WHERE id = ?",
                (ts, user_id),
            )
            self._conn.commit()

    def set_active(self, user_id: str, active: bool, *, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
                (1 if active else 0, ts, user_id),
            )
            self._conn.commit()

    def revoke_sessions_for_user(self, user_id: str, *, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (ts, user_id),
            )
            self._conn.commit()

    def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        token_hash: str,
        expires_at: float,
        ip: str | None,
        user_agent: str | None,
        now: float | None = None,
    ) -> AuthSession:
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    id, user_id, token_hash, created_at, expires_at, revoked_at,
                    last_seen_at, ip, user_agent
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (session_id, user_id, token_hash, ts, expires_at, ts, ip, user_agent),
            )
            self._conn.commit()
        return AuthSession(
            session_id=session_id,
            user_id=user_id,
            token_hash=token_hash,
            created_at=ts,
            expires_at=expires_at,
            revoked_at=None,
            last_seen_at=ts,
            ip=ip,
            user_agent=user_agent,
        )

    def get_session(self, session_id: str) -> AuthSession | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return AuthSession(
            session_id=str(row["id"]),
            user_id=str(row["user_id"]),
            token_hash=str(row["token_hash"]),
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            revoked_at=float(row["revoked_at"]) if row["revoked_at"] is not None else None,
            last_seen_at=float(row["last_seen_at"]) if row["last_seen_at"] is not None else None,
            ip=row["ip"],
            user_agent=row["user_agent"],
        )

    def touch_session(self, session_id: str, *, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (ts, session_id))
            self._conn.commit()

    def revoke_session(self, session_id: str, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (ts, session_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def create_one_time_token(
        self,
        table: str,
        *,
        user_id: str,
        token_hash: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> None:
        if table not in ("password_reset_tokens", "email_verify_tokens"):
            raise ValueError("unsupported token table")
        ts = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                f"""
                INSERT INTO {table} (id, user_id, token_hash, created_at, expires_at, used_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (str(uuid.uuid4()), user_id, token_hash, ts, ts + ttl_seconds),
            )
            self._conn.commit()

    def consume_one_time_token(
        self,
        table: str,
        token_hash: str,
        *,
        now: float | None = None,
    ) -> str | None:
        if table not in ("password_reset_tokens", "email_verify_tokens"):
            raise ValueError("unsupported token table")
        ts = now if now is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT id, user_id, expires_at, used_at FROM {table}
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None or row["used_at"] is not None or float(row["expires_at"]) <= ts:
                return None
            self._conn.execute(
                f"UPDATE {table} SET used_at = ? WHERE id = ?",
                (ts, row["id"]),
            )
            self._conn.commit()
            return str(row["user_id"])

    def claim_slot(self, farm_slot_id: int, user_id: str, rental_id: str | None, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT user_id FROM slot_ownership WHERE farm_slot_id = ?",
                (farm_slot_id,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO slot_ownership (farm_slot_id, user_id, rental_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (farm_slot_id, user_id, rental_id, ts, ts),
                )
                self._conn.commit()
                return True
            if str(existing["user_id"]) != user_id:
                return False
            self._conn.execute(
                "UPDATE slot_ownership SET rental_id = ?, updated_at = ? WHERE farm_slot_id = ?",
                (rental_id, ts, farm_slot_id),
            )
            self._conn.commit()
            return True

    def owner_of_slot(self, farm_slot_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id FROM slot_ownership WHERE farm_slot_id = ?",
                (farm_slot_id,),
            ).fetchone()
        return str(row["user_id"]) if row else None

    def list_owned_slots(self, user_id: str) -> list[SlotOwnership]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM slot_ownership WHERE user_id = ? ORDER BY farm_slot_id",
                (user_id,),
            ).fetchall()
        return [
            SlotOwnership(
                farm_slot_id=int(r["farm_slot_id"]),
                user_id=str(r["user_id"]),
                rental_id=r["rental_id"],
                created_at=float(r["created_at"]),
                updated_at=float(r["updated_at"]),
            )
            for r in rows
        ]

    def record_esim_upload(
        self,
        *,
        user_id: str,
        farm_slot_id: int,
        storage_key: str,
        rental_id: str | None,
        carrier: str | None,
        job_id: str | None,
        now: float | None = None,
    ) -> str:
        ts = now if now is not None else time.time()
        record_id = str(uuid.uuid4())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO esim_uploads (
                    id, user_id, farm_slot_id, storage_key, rental_id, carrier, job_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record_id, user_id, farm_slot_id, storage_key, rental_id, carrier, job_id, ts),
            )
            self._conn.commit()
        return record_id

    def close(self) -> None:
        self._conn.close()
