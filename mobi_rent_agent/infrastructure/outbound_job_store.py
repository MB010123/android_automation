"""VPS-side outbound job state linked to inbound webhook events."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class OutboundJobRecord:
    idempotency_key: str
    inbound_event_id: str
    inbound_row_id: int | None
    job_id: str
    sender_slot_id: int
    to_slot_id: int | None
    to_number_redacted: str
    status: str
    provider_message_id: str | None
    error: str | None
    created_at: float
    updated_at: float
    dispatch_attempts: int


class OutboundJobStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "outbound_job_schema_version" not in tables:
            self._conn.execute(
                "CREATE TABLE outbound_job_schema_version (version INTEGER NOT NULL)"
            )
            self._conn.execute(
                "INSERT INTO outbound_job_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE outbound_jobs (
                    idempotency_key TEXT PRIMARY KEY,
                    inbound_event_id TEXT NOT NULL UNIQUE,
                    inbound_row_id INTEGER,
                    job_id TEXT NOT NULL,
                    sender_slot_id INTEGER NOT NULL,
                    to_slot_id INTEGER,
                    to_number_redacted TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_message_id TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    dispatch_attempts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.commit()

    def get_by_inbound_event(self, inbound_event_id: str) -> OutboundJobRecord | None:
        row = self._conn.execute(
            "SELECT * FROM outbound_jobs WHERE inbound_event_id = ?",
            (inbound_event_id,),
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_idempotency(self, idempotency_key: str) -> OutboundJobRecord | None:
        row = self._conn.execute(
            "SELECT * FROM outbound_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return _row_to_record(row) if row else None

    def insert(self, record: OutboundJobRecord) -> None:
        try:
            self._insert_row(record)
        except sqlite3.IntegrityError:
            self._conn.rollback()
            raise

    def _insert_row(self, record: OutboundJobRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO outbound_jobs (
                idempotency_key, inbound_event_id, inbound_row_id, job_id,
                sender_slot_id, to_slot_id, to_number_redacted, status,
                provider_message_id, error, created_at, updated_at, dispatch_attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.idempotency_key,
                record.inbound_event_id,
                record.inbound_row_id,
                record.job_id,
                record.sender_slot_id,
                record.to_slot_id,
                record.to_number_redacted,
                record.status,
                record.provider_message_id,
                record.error,
                record.created_at,
                record.updated_at,
                record.dispatch_attempts,
            ),
        )
        self._conn.commit()

    def update(
        self,
        idempotency_key: str,
        *,
        status: str | None = None,
        provider_message_id: str | None = None,
        error: str | None = None,
        dispatch_attempts: int | None = None,
    ) -> None:
        now = time.time()
        fields: list[str] = ["updated_at = ?"]
        values: list[object] = [now]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if provider_message_id is not None:
            fields.append("provider_message_id = ?")
            values.append(provider_message_id)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if dispatch_attempts is not None:
            fields.append("dispatch_attempts = ?")
            values.append(dispatch_attempts)
        values.append(idempotency_key)
        self._conn.execute(
            f"UPDATE outbound_jobs SET {', '.join(fields)} WHERE idempotency_key = ?",
            values,
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _row_to_record(row: sqlite3.Row) -> OutboundJobRecord:
    return OutboundJobRecord(
        idempotency_key=row["idempotency_key"],
        inbound_event_id=row["inbound_event_id"],
        inbound_row_id=row["inbound_row_id"],
        job_id=row["job_id"],
        sender_slot_id=int(row["sender_slot_id"]),
        to_slot_id=int(row["to_slot_id"]) if row["to_slot_id"] is not None else None,
        to_number_redacted=row["to_number_redacted"],
        status=row["status"],
        provider_message_id=row["provider_message_id"],
        error=row["error"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        dispatch_attempts=int(row["dispatch_attempts"]),
    )
