"""VPS Lovable API: enqueue outbound SMS to a farm slot (async dispatch)."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import requests

from infrastructure.farm_sms_client import FarmSmsClient
from infrastructure.outbound_message_store import OutboundMessageRecord, OutboundMessageStore
from infrastructure.redact import redact_phone
from infrastructure.sms_destination import idempotency_fingerprint, validate_sms_destination
from infrastructure.slot_public_id import farm_slot_for_public_id, public_id_for_farm_slot
from infrastructure.vps_rate_limiter import VpsRateLimiter

logger = logging.getLogger("vps_backend.slot_sms")

MAX_BODY_LEN = 1600
MAX_IDEMPOTENCY_LEN = 128


@dataclass
class SlotSmsEnqueueResult:
    http_status: int
    body: dict[str, Any]


class VpsSlotSmsService:
    def __init__(
        self,
        *,
        message_store: OutboundMessageStore,
        farm_client: FarmSmsClient | None,
        farm_status_fetcher: Callable[[], dict[str, Any]],
        known_farm_slots: set[int],
        slot_id_overrides: dict[str, int] | None = None,
        rate_limiter: VpsRateLimiter | None = None,
        max_dispatch_attempts: int = 3,
    ) -> None:
        self._store = message_store
        self._farm = farm_client
        self._farm_status = farm_status_fetcher
        self._known_slots = known_farm_slots
        self._overrides = slot_id_overrides or {}
        self._rate = rate_limiter or VpsRateLimiter()
        self._max_attempts = max(1, max_dispatch_attempts)
        self._lock = threading.Lock()

    def enqueue_send(
        self,
        slot_public_id: str,
        payload: dict[str, Any],
    ) -> SlotSmsEnqueueResult:
        farm_slot = farm_slot_for_public_id(slot_public_id, self._overrides)
        if farm_slot is None or farm_slot not in self._known_slots:
            return SlotSmsEnqueueResult(404, {"error": "slot_not_found"})

        idem = str(payload.get("idempotency_key") or "").strip()
        if not idem:
            return SlotSmsEnqueueResult(400, {"error": "missing_idempotency_key"})
        if len(idem) > MAX_IDEMPOTENCY_LEN:
            return SlotSmsEnqueueResult(400, {"error": "invalid_idempotency_key"})

        to_number = validate_sms_destination(payload.get("to"))
        if not to_number:
            return SlotSmsEnqueueResult(400, {"error": "invalid_destination"})

        body = payload.get("body")
        if body is None or not str(body).strip():
            return SlotSmsEnqueueResult(400, {"error": "invalid_message"})
        body_text = str(body)
        if len(body_text) > MAX_BODY_LEN:
            return SlotSmsEnqueueResult(400, {"error": "invalid_message"})

        fingerprint = idempotency_fingerprint(to_number, body_text)
        existing = self._store.get_by_slot_idempotency(farm_slot, idem)
        if existing is not None:
            if existing.content_fingerprint != fingerprint:
                return SlotSmsEnqueueResult(409, {"error": "idempotency_conflict"})
            return SlotSmsEnqueueResult(
                202,
                {
                    "message_id": existing.message_id,
                    "status": existing.status,
                    "slot_id": existing.slot_public_id,
                },
            )

        allowed, retry_after = self._rate.check(farm_slot)
        if not allowed:
            return SlotSmsEnqueueResult(
                429,
                {"error": "rate_limited", "retry_after": retry_after},
            )

        try:
            farm = self._farm_status()
        except (requests.RequestException, OSError, ConnectionError):
            logger.warning("farm_unreachable message_id=pending slot=%s", farm_slot)
            return SlotSmsEnqueueResult(503, {"error": "farm_unreachable"})
        if not farm.get("ok"):
            return SlotSmsEnqueueResult(503, {"error": "farm_unreachable"})
        offline = farm.get("offline_slots") or []
        if farm_slot in offline:
            return SlotSmsEnqueueResult(409, {"error": "device_offline"})

        if self._farm is None:
            return SlotSmsEnqueueResult(503, {"error": "farm_unreachable"})

        message_id = str(uuid.uuid4())
        public_id = public_id_for_farm_slot(farm_slot)
        now = time.time()
        record = OutboundMessageRecord(
            message_id=message_id,
            slot_public_id=public_id,
            farm_slot_id=farm_slot,
            direction="out",
            to_number=to_number,
            body=body_text,
            status="queued",
            error_code=None,
            idempotency_key=idem,
            content_fingerprint=fingerprint,
            provider_message_id=None,
            created_at=now,
            updated_at=now,
            dispatch_attempts=0,
        )
        try:
            self._store.insert(record)
        except sqlite3.IntegrityError:
            raced = self._store.get_by_slot_idempotency(farm_slot, idem)
            if raced is None:
                return SlotSmsEnqueueResult(500, {"error": "internal_error"})
            if raced.content_fingerprint != fingerprint:
                return SlotSmsEnqueueResult(409, {"error": "idempotency_conflict"})
            return SlotSmsEnqueueResult(
                202,
                {
                    "message_id": raced.message_id,
                    "status": raced.status,
                    "slot_id": raced.slot_public_id,
                },
            )

        logger.info(
            "slot_sms_queued message_id=%s slot=%s dest=%s",
            message_id,
            farm_slot,
            redact_phone(to_number),
        )
        threading.Thread(
            target=self._dispatch_async,
            args=(message_id,),
            daemon=True,
            name=f"slot-sms-{message_id[:8]}",
        ).start()
        return SlotSmsEnqueueResult(
            202,
            {"message_id": message_id, "status": "queued", "slot_id": public_id},
        )

    def _dispatch_async(self, message_id: str) -> None:
        record = self._store.get_by_message_id(message_id)
        if record is None or record.status not in ("queued", "failed"):
            return
        farm_key = f"slot-api-{record.farm_slot_id}-{record.idempotency_key}"
        last_error: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._store.update(message_id, status="queued", dispatch_attempts=attempt)
            if self._farm is None:
                last_error = "farm_not_configured"
                break
            response = self._farm.send_sms(
                job_id=message_id,
                idempotency_key=farm_key,
                sender_slot_id=record.farm_slot_id,
                body=record.body,
                to_number=record.to_number,
            )
            if response.ok:
                status = str(response.body.get("status") or "sent")
                if status in ("accepted", "queued", "sending"):
                    status = "sent"
                provider_id = response.body.get("provider_message_id")
                self._store.update(
                    message_id,
                    status=status,
                    provider_message_id=str(provider_id) if provider_id else None,
                    error_code=None,
                )
                logger.info(
                    "slot_sms_dispatch_ok message_id=%s status=%s attempt=%s",
                    message_id,
                    status,
                    attempt,
                )
                return
            last_error = response.error or f"http_{response.http_status}"
            if response.http_status in (401, 403, 400, 422):
                break
            time.sleep(min(2.0 * attempt, 10.0))
        self._store.update(message_id, status="failed", error_code=last_error or "dispatch_failed")
        logger.warning(
            "slot_sms_dispatch_failed message_id=%s error=%s",
            message_id,
            last_error,
        )

    def get_message(self, message_id: str, *, farm_slot_filter: int | None = None) -> SlotSmsEnqueueResult:
        record = self._store.get_by_message_id(message_id)
        if record is None:
            return SlotSmsEnqueueResult(404, {"error": "not_found"})
        if farm_slot_filter is not None and record.farm_slot_id != farm_slot_filter:
            return SlotSmsEnqueueResult(404, {"error": "not_found"})
        return SlotSmsEnqueueResult(200, _message_json(record))

    def list_messages(
        self,
        slot_public_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
        direction: str | None = None,
    ) -> SlotSmsEnqueueResult:
        farm_slot = farm_slot_for_public_id(slot_public_id, self._overrides)
        if farm_slot is None or farm_slot not in self._known_slots:
            return SlotSmsEnqueueResult(404, {"error": "slot_not_found"})
        cursor_at: float | None = None
        cursor_id: str | None = None
        if cursor:
            parts = cursor.split("|", 1)
            if len(parts) == 2:
                try:
                    cursor_at = float(parts[0])
                    cursor_id = parts[1]
                except ValueError:
                    return SlotSmsEnqueueResult(400, {"error": "invalid_cursor"})
        records, next_cursor = self._store.list_for_slot(
            farm_slot,
            limit=limit,
            cursor_created_at=cursor_at,
            cursor_message_id=cursor_id,
            direction=direction,
        )
        return SlotSmsEnqueueResult(
            200,
            {
                "messages": [_message_json(r) for r in records],
                "next_cursor": next_cursor,
            },
        )


def _message_json(record: OutboundMessageRecord) -> dict[str, Any]:
    return {
        "message_id": record.message_id,
        "slot_id": record.slot_public_id,
        "direction": record.direction,
        "to": record.to_number,
        "body": record.body,
        "status": record.status,
        "error_code": record.error_code,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
