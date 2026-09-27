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

SlotEventRecorder = Callable[[int, str, str], None]

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
        event_recorder: SlotEventRecorder | None = None,
        tenant_store: Any = None,
    ) -> None:
        self._store = message_store
        self._farm = farm_client
        self._farm_status = farm_status_fetcher
        self._known_slots = known_farm_slots
        self._overrides = slot_id_overrides or {}
        self._rate = rate_limiter or VpsRateLimiter()
        self._max_attempts = max(1, max_dispatch_attempts)
        self._lock = threading.Lock()
        self._record_event = event_recorder
        self._tenant = tenant_store

    def _resolve_farm_slot(self, slot_public_id: str) -> int | None:
        farm_slot = farm_slot_for_public_id(slot_public_id, self._overrides)
        if farm_slot is not None:
            return farm_slot
        resolver = getattr(self._tenant, "farm_slot_for_slot_id", None)
        if not callable(resolver):
            return None
        try:
            return resolver(slot_public_id)
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("tenant_slot_id_lookup_failed")
            return None

    def enqueue_send(
        self,
        slot_public_id: str,
        payload: dict[str, Any],
    ) -> SlotSmsEnqueueResult:
        farm_slot = self._resolve_farm_slot(slot_public_id)
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
        if self._record_event is not None:
            self._record_event(farm_slot, "sms_send_requested", f"message_id={message_id}")
        self._mirror_tenant_message(
            farm_slot_id=farm_slot,
            message_id=message_id,
            direction="outbound",
            phone_number=to_number,
            message_body=body_text,
            status="queued",
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
                if self._record_event is not None:
                    self._record_event(
                        record.farm_slot_id,
                        "sms_sent",
                        f"message_id={message_id} status={status}",
                    )
                self._touch_tenant_status(message_id, status)
                return
            last_error = response.error or f"http_{response.http_status}"
            if response.http_status in (401, 403, 400, 422):
                break
            time.sleep(min(2.0 * attempt, 10.0))
        self._store.update(message_id, status="failed", error_code=last_error or "dispatch_failed")
        self._touch_tenant_status(message_id, "failed")
        if self._record_event is not None and record is not None:
            self._record_event(
                record.farm_slot_id,
                "sms_failed",
                f"message_id={message_id} error={last_error or 'dispatch_failed'}",
            )
        logger.warning(
            "slot_sms_dispatch_failed message_id=%s error=%s",
            message_id,
            last_error,
        )

    def get_message(self, message_id: str, *, farm_slot_filter: int | None = None) -> SlotSmsEnqueueResult:
        """Farm/operator lookup of the operational dispatch queue. Not tenant history."""
        record = self._store.get_by_message_id(message_id)
        if record is None:
            return SlotSmsEnqueueResult(404, {"error": "not_found"})
        if farm_slot_filter is not None and record.farm_slot_id != farm_slot_filter:
            return SlotSmsEnqueueResult(404, {"error": "not_found"})
        return SlotSmsEnqueueResult(200, _message_json(record))

    def get_tenant_message(self, message_id: str, *, user_id: str) -> SlotSmsEnqueueResult:
        """Authoritative tenant history from Supabase. SQLite queue rows are ignored."""
        getter = getattr(self._tenant, "get_message", None)
        if not callable(getter):
            return SlotSmsEnqueueResult(404, {"error": "not_found"})
        if getattr(self._tenant, "privileged", True) is False:
            return SlotSmsEnqueueResult(503, {"error": "auth_not_configured"})
        try:
            row = getter(message_id)
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("supabase_message_get_failed")
            return SlotSmsEnqueueResult(503, {"error": "auth_unavailable"})
        if not isinstance(row, dict):
            return SlotSmsEnqueueResult(404, {"error": "not_found"})
        slot_ref = str(row.get("slot_id") or "").strip()
        farm_slot = self._farm_slot_for_tenant_row(slot_ref)
        if farm_slot is None or farm_slot not in self._known_slots:
            return SlotSmsEnqueueResult(404, {"error": "not_found"})
        owner = getattr(self._tenant, "owner_of_slot", None)
        if not callable(owner) or owner(farm_slot) != user_id:
            return SlotSmsEnqueueResult(404, {"error": "not_found"})
        return SlotSmsEnqueueResult(200, _tenant_message_json(row, public_id_for_farm_slot(farm_slot)))

    def _farm_slot_for_tenant_row(self, slot_ref: str) -> int | None:
        if not slot_ref:
            return None
        mapped = farm_slot_for_public_id(slot_ref, self._overrides)
        if mapped is not None:
            return mapped
        resolver = getattr(self._tenant, "farm_slot_for_slot_id", None)
        if callable(resolver):
            try:
                return resolver(slot_ref)
            except (TypeError, ValueError, OSError, requests.RequestException):
                return None
        return None

    def list_messages(
        self,
        slot_public_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
        direction: str | None = None,
    ) -> SlotSmsEnqueueResult:
        farm_slot = self._resolve_farm_slot(slot_public_id)
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
        tenant_rows = self._list_tenant_messages(farm_slot, limit=limit, direction=direction)
        if self._tenant is not None:
            if getattr(self._tenant, "privileged", True) is False:
                return SlotSmsEnqueueResult(503, {"error": "auth_not_configured"})
            if tenant_rows is None:
                return SlotSmsEnqueueResult(503, {"error": "auth_unavailable"})
            public_id = public_id_for_farm_slot(farm_slot)
            return SlotSmsEnqueueResult(
                200,
                {
                    "messages": [_tenant_message_json(row, public_id) for row in tenant_rows],
                    "next_cursor": None,
                },
            )
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

    def _mirror_tenant_message(
        self,
        *,
        farm_slot_id: int,
        message_id: str,
        direction: str,
        phone_number: str | None,
        message_body: str | None,
        status: str,
        job_id: str | None = None,
    ) -> None:
        writer = getattr(self._tenant, "record_message", None)
        if not callable(writer):
            return
        try:
            writer(
                farm_slot_id=farm_slot_id,
                message_id=message_id,
                direction=direction,
                phone_number=phone_number,
                message_body=message_body,
                status=status,
                job_id=job_id,
            )
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("supabase_message_mirror_failed slot=%s", farm_slot_id)

    def _touch_tenant_status(self, message_id: str, status: str) -> None:
        updater = getattr(self._tenant, "update_message_status", None)
        if not callable(updater):
            return
        try:
            updater(message_id, status)
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("supabase_message_status_failed")

    def _list_tenant_messages(
        self,
        farm_slot: int,
        *,
        limit: int,
        direction: str | None,
    ) -> list[dict[str, Any]] | None:
        lister = getattr(self._tenant, "list_messages", None)
        if not callable(lister):
            return None
        mapped = None
        if direction in {"out", "outbound"}:
            mapped = "outbound"
        elif direction in {"in", "inbound"}:
            mapped = "inbound"
        try:
            return lister(farm_slot, limit=limit, direction=mapped)
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("supabase_message_list_failed slot=%s", farm_slot)
            return None


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


def _tenant_message_json(row: dict[str, Any], slot_public_id: str) -> dict[str, Any]:
    direction = str(row.get("direction") or "")
    if direction == "outbound":
        direction = "out"
    elif direction == "inbound":
        direction = "in"
    return {
        "message_id": row.get("message_id"),
        "slot_id": slot_public_id,
        "direction": direction,
        "to": row.get("phone_number"),
        "body": row.get("message_body"),
        "status": row.get("status"),
        "error_code": None,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
