"""Durable SQLite store for SMS outbox records (stdlib sqlite3)."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from domain.farm import OutboundMessageRecord, OutboundMessageStatus, SmsFinalStatus

SCHEMA_VERSION = 1


class SmsOutboxStoreError(RuntimeError):
    """The outbox database is missing or corrupt."""


class SqliteSmsOutboxStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def load_all(self) -> dict[str, OutboundMessageRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM outbound_messages").fetchall()
        return {row["idempotency_key"]: _row_to_record(row) for row in rows}

    def upsert(self, record: OutboundMessageRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO outbound_messages (
                    idempotency_key, job_id, slot_id, status, final_status,
                    to_number_redacted, destination_redacted, provider_message_id,
                    voidfix_device_id, voidfix_sim_slot, accepted_at,
                    provider_sent_date, provider_delivered_date, provider_error_code,
                    last_polled_at, error, blocked_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    job_id=excluded.job_id,
                    slot_id=excluded.slot_id,
                    status=excluded.status,
                    final_status=excluded.final_status,
                    to_number_redacted=excluded.to_number_redacted,
                    destination_redacted=excluded.destination_redacted,
                    provider_message_id=excluded.provider_message_id,
                    voidfix_device_id=excluded.voidfix_device_id,
                    voidfix_sim_slot=excluded.voidfix_sim_slot,
                    accepted_at=excluded.accepted_at,
                    provider_sent_date=excluded.provider_sent_date,
                    provider_delivered_date=excluded.provider_delivered_date,
                    provider_error_code=excluded.provider_error_code,
                    last_polled_at=excluded.last_polled_at,
                    error=excluded.error,
                    blocked_reason=excluded.blocked_reason,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
                """,
                _record_to_row(record),
            )
            self._conn.commit()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
                self._conn.commit()
            elif int(row["version"]) != SCHEMA_VERSION:
                raise SmsOutboxStoreError(
                    f"sms outbox schema version {row['version']} != supported {SCHEMA_VERSION}"
                )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_messages (
                    idempotency_key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    final_status TEXT,
                    to_number_redacted TEXT NOT NULL,
                    destination_redacted TEXT,
                    provider_message_id TEXT,
                    voidfix_device_id TEXT,
                    voidfix_sim_slot INTEGER,
                    accepted_at REAL,
                    provider_sent_date TEXT,
                    provider_delivered_date TEXT,
                    provider_error_code TEXT,
                    last_polled_at REAL,
                    error TEXT,
                    blocked_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.commit()


def _record_to_row(record: OutboundMessageRecord) -> tuple:
    return (
        record.idempotency_key,
        record.job_id,
        int(record.slot_id),
        record.status.value,
        record.final_status.value if record.final_status else None,
        record.to_number_redacted,
        record.destination_redacted,
        record.provider_message_id,
        record.voidfix_device_id,
        record.voidfix_sim_slot,
        record.accepted_at,
        record.provider_sent_date,
        record.provider_delivered_date,
        record.provider_error_code,
        record.last_polled_at,
        record.error,
        record.blocked_reason,
        record.created_at,
        record.updated_at,
    )


def _row_to_record(row: sqlite3.Row) -> OutboundMessageRecord:
    final_raw = row["final_status"]
    return OutboundMessageRecord(
        idempotency_key=row["idempotency_key"],
        job_id=row["job_id"],
        slot_id=int(row["slot_id"]),
        status=OutboundMessageStatus(row["status"]),
        to_number_redacted=row["to_number_redacted"],
        provider_message_id=row["provider_message_id"],
        voidfix_device_id=row["voidfix_device_id"],
        voidfix_sim_slot=row["voidfix_sim_slot"],
        destination_redacted=row["destination_redacted"],
        accepted_at=row["accepted_at"],
        provider_sent_date=row["provider_sent_date"],
        provider_delivered_date=row["provider_delivered_date"],
        provider_error_code=row["provider_error_code"],
        final_status=SmsFinalStatus(final_raw) if final_raw else None,
        last_polled_at=row["last_polled_at"],
        error=row["error"],
        blocked_reason=row["blocked_reason"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
