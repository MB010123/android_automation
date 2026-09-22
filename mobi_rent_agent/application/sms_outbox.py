"""SMS outbox with idempotency; optional SQLite durability across restarts."""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING

from domain.farm import (
    OutboundMessageRecord,
    OutboundMessageStatus,
    SmsFinalStatus,
    is_terminal_sms_final_status,
)
from infrastructure.redact import redact_phone

if TYPE_CHECKING:
    from infrastructure.sms_outbox_store import SqliteSmsOutboxStore


class DuplicateIdempotencyError(ValueError):
    """The idempotency key already belongs to another outbound attempt."""


class SmsOutbox:
    def __init__(self, store: SqliteSmsOutboxStore | None = None) -> None:
        self._lock = threading.Lock()
        self._store = store
        self._records: dict[str, OutboundMessageRecord] = {}
        if self._store is not None:
            self._records = self._store.load_all()

    def reserve(
        self,
        *,
        slot_id: int,
        to_number: str,
        idempotency_key: str | None = None,
        job_id: str | None = None,
        now: float | None = None,
    ) -> OutboundMessageRecord:
        key = idempotency_key or str(uuid.uuid4())
        stamp = now if now is not None else time.time()
        redacted = redact_phone(to_number)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                raise DuplicateIdempotencyError(
                    f"idempotency key already used for slot {existing.slot_id} "
                    f"status={existing.status.value}"
                )
            record = OutboundMessageRecord(
                idempotency_key=key,
                job_id=job_id or key,
                slot_id=slot_id,
                status=OutboundMessageStatus.PENDING,
                to_number_redacted=redacted,
                destination_redacted=redacted,
                final_status=SmsFinalStatus.QUEUED,
                created_at=stamp,
                updated_at=stamp,
            )
            self._records[key] = record
            self._persist_locked(record)
            return record

    def get(self, idempotency_key: str) -> OutboundMessageRecord | None:
        with self._lock:
            return self._records.get(idempotency_key)

    def list_records(self) -> list[OutboundMessageRecord]:
        with self._lock:
            return list(self._records.values())

    def list_non_terminal(self) -> list[OutboundMessageRecord]:
        with self._lock:
            return [
                record
                for record in self._records.values()
                if not is_terminal_sms_final_status(record.final_status)
            ]

    def update(
        self,
        idempotency_key: str,
        *,
        status: OutboundMessageStatus,
        provider_message_id: str | None = None,
        voidfix_device_id: str | None = None,
        voidfix_sim_slot: int | None = None,
        destination_redacted: str | None = None,
        accepted_at: float | None = None,
        provider_sent_date: str | None = None,
        provider_delivered_date: str | None = None,
        provider_error_code: str | None = None,
        final_status: SmsFinalStatus | None = None,
        last_polled_at: float | None = None,
        error: str | None = None,
        blocked_reason: str | None = None,
        now: float | None = None,
    ) -> OutboundMessageRecord:
        stamp = now if now is not None else time.time()
        with self._lock:
            current = self._records[idempotency_key]
            updated = OutboundMessageRecord(
                idempotency_key=current.idempotency_key,
                job_id=current.job_id,
                slot_id=current.slot_id,
                status=status,
                to_number_redacted=current.to_number_redacted,
                provider_message_id=provider_message_id or current.provider_message_id,
                voidfix_device_id=voidfix_device_id or current.voidfix_device_id,
                voidfix_sim_slot=(
                    voidfix_sim_slot if voidfix_sim_slot is not None else current.voidfix_sim_slot
                ),
                destination_redacted=destination_redacted or current.destination_redacted,
                accepted_at=accepted_at if accepted_at is not None else current.accepted_at,
                provider_sent_date=provider_sent_date or current.provider_sent_date,
                provider_delivered_date=provider_delivered_date or current.provider_delivered_date,
                provider_error_code=provider_error_code or current.provider_error_code,
                final_status=final_status or current.final_status,
                last_polled_at=last_polled_at if last_polled_at is not None else current.last_polled_at,
                error=error if error is not None else current.error,
                blocked_reason=blocked_reason if blocked_reason is not None else current.blocked_reason,
                created_at=current.created_at,
                updated_at=stamp,
            )
            self._records[idempotency_key] = updated
            self._persist_locked(updated)
            return updated

    def _persist_locked(self, record: OutboundMessageRecord) -> None:
        if self._store is not None:
            self._store.upsert(record)
