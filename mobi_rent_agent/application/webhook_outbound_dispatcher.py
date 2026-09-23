"""Dispatch outbound SMS jobs to Farm Agent after inbound webhook storage."""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
import uuid
from typing import Any

from application.webhook_reply_policy import WebhookReplyPolicy, load_webhook_reply_policy, rule_for_inbound_slot
from domain.models import InboundSms
from infrastructure.farm_sms_client import FarmSmsClient
from infrastructure.outbound_job_store import OutboundJobRecord, OutboundJobStore
from infrastructure.redact import redact_phone

logger = logging.getLogger("vps_backend.dispatcher")


def inbound_event_id(msg: InboundSms, raw_row: dict[str, Any] | None = None) -> str:
    if raw_row and raw_row.get("ID") is not None:
        return f"voidfix:{raw_row.get('ID')}"
    basis = "|".join(
        [
            str(msg.device_id or ""),
            str(msg.from_number or ""),
            str(msg.message or ""),
            str(msg.received_at or ""),
        ]
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
    return f"hash:{digest}"


def _coerce_raw_message(payload: object, index: int) -> dict[str, Any] | None:
    if isinstance(payload, list) and index < len(payload) and isinstance(payload[index], dict):
        return payload[index]
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list) and index < len(messages) and isinstance(messages[index], dict):
            return messages[index]
    return None


class WebhookOutboundDispatcher:
    def __init__(
        self,
        *,
        job_store: OutboundJobStore,
        farm_client: FarmSmsClient | None,
        policy: WebhookReplyPolicy | None = None,
        max_dispatch_attempts: int = 3,
    ) -> None:
        self._jobs = job_store
        self._farm = farm_client
        self._policy = policy or load_webhook_reply_policy()
        self._max_attempts = max(1, max_dispatch_attempts)

    def process_inbound(
        self,
        msg: InboundSms,
        *,
        inbound_row_id: int | None,
        inbound_slot: int | None,
        payload: object,
        index: int,
        body_override: str | None = None,
    ) -> dict[str, Any]:
        raw = _coerce_raw_message(payload, index)
        event_id = inbound_event_id(msg, raw)
        existing = self._jobs.get_by_inbound_event(event_id)
        if existing is not None:
            logger.info("duplicate_event_ignored inbound_event_id=%s", event_id)
            return {
                "dispatched": False,
                "duplicate": True,
                "outbound_job_id": existing.job_id,
                "status": existing.status,
                "provider_message_id": existing.provider_message_id,
            }

        rule = rule_for_inbound_slot(self._policy, inbound_slot)
        if rule is None:
            return {"dispatched": False, "reason": "no_reply_rule"}

        if self._farm is None:
            return {"dispatched": False, "reason": "farm_agent_not_configured"}

        job_id = str(uuid.uuid4())
        idempotency_key = f"webhook-reply-{event_id}"
        now = time.time()
        record = OutboundJobRecord(
            idempotency_key=idempotency_key,
            inbound_event_id=event_id,
            inbound_row_id=inbound_row_id,
            job_id=job_id,
            sender_slot_id=rule.sender_slot,
            to_slot_id=rule.reply_to_slot,
            to_number_redacted="+XXX****",
            status="queued",
            provider_message_id=None,
            error=None,
            created_at=now,
            updated_at=now,
            dispatch_attempts=0,
        )
        try:
            self._jobs.insert(record)
        except sqlite3.IntegrityError:
            raced = self._jobs.get_by_inbound_event(event_id)
            if raced is not None:
                logger.info("duplicate_event_ignored inbound_event_id=%s", event_id)
                return {
                    "dispatched": False,
                    "duplicate": True,
                    "outbound_job_id": raced.job_id,
                    "status": raced.status,
                    "provider_message_id": raced.provider_message_id,
                }
            raise
        logger.info("outbound_job_created job_id=%s inbound_event_id=%s", job_id, event_id)

        body = body_override or msg.message
        prefix = os.getenv("WEBHOOK_AUTO_REPLY_BODY_PREFIX", "").strip()
        if prefix:
            body = f"{prefix} {body}".strip()

        last_error: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._jobs.update(
                idempotency_key,
                status="sending",
                dispatch_attempts=attempt,
            )
            response = self._farm.send_sms(
                job_id=job_id,
                idempotency_key=idempotency_key,
                sender_slot_id=rule.sender_slot,
                body=body,
                to_slot_id=rule.reply_to_slot,
            )
            if response.ok:
                status = str(response.body.get("status") or "accepted")
                provider_id = response.body.get("provider_message_id")
                self._jobs.update(
                    idempotency_key,
                    status=status,
                    provider_message_id=str(provider_id) if provider_id else None,
                    error=None,
                )
                return {
                    "dispatched": True,
                    "outbound_job_id": job_id,
                    "idempotency_key": idempotency_key,
                    "status": status,
                    "provider_message_id": provider_id,
                    "duplicate": bool(response.body.get("duplicate")),
                }
            last_error = response.error or f"http_{response.http_status}"
            if response.http_status in (401, 403, 400, 422):
                break
            time.sleep(min(2.0 * attempt, 10.0))

        self._jobs.update(
            idempotency_key,
            status="failed",
            error=last_error,
        )
        logger.warning(
            "outbound_job_failed job_id=%s error=%s",
            job_id,
            last_error,
        )
        return {
            "dispatched": False,
            "outbound_job_id": job_id,
            "status": "failed",
            "error": last_error,
        }
