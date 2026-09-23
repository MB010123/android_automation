"""VPS API outbound SMS messages (Lovable → farm slot)."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class OutboundMessageRecord:
    message_id: str
    slot_public_id: str
    farm_slot_id: int
    direction: str
    to_number: str
    body: str
    status: str
    error_code: str | None
    idempotency_key: str
    content_fingerprint: str
    provider_message_id: str | None
    created_at: float
    updated_at: float
    dispatch_attempts: int


class OutboundMessageStore:
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
        if "outbound_api_schema_version" not in tables:
            self._conn.execute(
                "CREATE TABLE outbound_api_schema_version (version INTEGER NOT NULL)"
            )
            self._conn.execute(
                "INSERT INTO outbound_api_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE outbound_api_messages (
                    message_id TEXT PRIMARY KEY,
                    slot_public_id TEXT NOT NULL,
                    farm_slot_id INTEGER NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'out',
                    to_number TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    idempotency_key TEXT NOT NULL,
                    content_fingerprint TEXT NOT NULL,
                    provider_message_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (farm_slot_id, idempotency_key)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX idx_outbound_api_slot_created "
                "ON outbound_api_messages (farm_slot_id, created_at DESC)"
            )
            self._conn.commit()

    def get_by_message_id(self, message_id: str) -> OutboundMessageRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM outbound_api_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_slot_idempotency(
        self, farm_slot_id: int, idempotency_key: str
    ) -> OutboundMessageRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM outbound_api_messages WHERE farm_slot_id = ? AND idempotency_key = ?",
                (farm_slot_id, idempotency_key),
            ).fetchone()
        return _row_to_record(row) if row else None

    def insert(self, record: OutboundMessageRecord) -> None:
        with self._lock:
            try:
                self._conn.execute(
                """
                INSERT INTO outbound_api_messages (
                    message_id, slot_public_id, farm_slot_id, direction, to_number, body,
                    status, error_code, idempotency_key, content_fingerprint,
                    provider_message_id, created_at, updated_at, dispatch_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.message_id,
                    record.slot_public_id,
                    record.farm_slot_id,
                    record.direction,
                    record.to_number,
                    record.body,
                    record.status,
                    record.error_code,
                    record.idempotency_key,
                    record.content_fingerprint,
                    record.provider_message_id,
                    record.created_at,
                    record.updated_at,
                    record.dispatch_attempts,
                ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                raise

    def update(
        self,
        message_id: str,
        *,
        status: str | None = None,
        error_code: str | None = None,
        provider_message_id: str | None = None,
        dispatch_attempts: int | None = None,
    ) -> None:
        now = time.time()
        fields: list[str] = ["updated_at = ?"]
        values: list[object] = [now]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if error_code is not None:
            fields.append("error_code = ?")
            values.append(error_code)
        if provider_message_id is not None:
            fields.append("provider_message_id = ?")
            values.append(provider_message_id)
        if dispatch_attempts is not None:
            fields.append("dispatch_attempts = ?")
            values.append(dispatch_attempts)
        values.append(message_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE outbound_api_messages SET {', '.join(fields)} WHERE message_id = ?",
                values,
            )
            self._conn.commit()

    def list_for_slot(
        self,
        farm_slot_id: int,
        *,
        limit: int = 50,
        cursor_created_at: float | None = None,
        cursor_message_id: str | None = None,
        direction: str | None = None,
    ) -> tuple[list[OutboundMessageRecord], str | None]:
        limit = max(1, min(int(limit), 100))
        query = "SELECT * FROM outbound_api_messages WHERE farm_slot_id = ?"
        params: list[object] = [farm_slot_id]
        if direction:
            query += " AND direction = ?"
            params.append(direction)
        if cursor_created_at is not None and cursor_message_id:
            query += " AND (created_at < ? OR (created_at = ? AND message_id < ?))"
            params.extend([cursor_created_at, cursor_created_at, cursor_message_id])
        query += " ORDER BY created_at DESC, message_id DESC LIMIT ?"
        params.append(limit + 1)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        records = [_row_to_record(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = records[-1]
            next_cursor = f"{last.created_at:.6f}|{last.message_id}"
        return records, next_cursor

    def close(self) -> None:
        self._conn.close()


def _row_to_record(row: sqlite3.Row) -> OutboundMessageRecord:
    return OutboundMessageRecord(
        message_id=row["message_id"],
        slot_public_id=row["slot_public_id"],
        farm_slot_id=int(row["farm_slot_id"]),
        direction=row["direction"],
        to_number=row["to_number"],
        body=row["body"],
        status=row["status"],
        error_code=row["error_code"],
        provider_message_id=row["provider_message_id"],
        idempotency_key=row["idempotency_key"],
        content_fingerprint=row["content_fingerprint"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        dispatch_attempts=int(row["dispatch_attempts"]),
    )
