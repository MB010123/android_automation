"""Farm Agent SMS send command (uses SmsDispatchService.send_for_slot)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from application.sms_factory import build_sms_dispatch_service
from domain.farm import SmsFinalStatus
from infrastructure.config import AgentConfig
from infrastructure.redact import redact_phone
from infrastructure.slot_msisdn_map import load_slot_msisdn_map

logger = logging.getLogger("farm_agent.sms_command")

MAX_BODY_LEN = 1600
SLOT_ID_RE = re.compile(r"^(?:slot-(\d{1,2})|(\d{1,2}))$")


@dataclass
class FarmSmsSendRequest:
    job_id: str
    idempotency_key: str
    sender_slot_id: int
    body: str
    to_number: str | None = None
    to_slot_id: int | None = None


@dataclass
class FarmSmsSendResult:
    ok: bool
    http_status: int
    job_id: str
    idempotency_key: str
    status: str
    provider_message_id: str | None = None
    error: str | None = None
    duplicate: bool = False


def parse_farm_sms_send_body(data: dict[str, Any]) -> FarmSmsSendRequest:
    job_id = str(data.get("job_id") or "").strip()
    idempotency_key = str(data.get("idempotency_key") or "").strip()
    if not job_id or not idempotency_key:
        raise ValueError("job_id and idempotency_key are required")
    raw_slot = data.get("sender_slot_id") or data.get("slot_id")
    if raw_slot is None:
        raise ValueError("sender_slot_id is required")
    sender_slot_id = _parse_slot_id(raw_slot)
    body = str(data.get("body") or "").strip()
    if not body:
        raise ValueError("body is required")
    if len(body) > MAX_BODY_LEN:
        raise ValueError("body too long")
    to_number = data.get("to")
    to_slot_id = data.get("to_slot_id")
    if to_number is not None:
        to_number = str(to_number).strip()
    if to_slot_id is not None:
        to_slot_id = int(to_slot_id)
    if not to_number and to_slot_id is None:
        raise ValueError("to or to_slot_id is required")
    return FarmSmsSendRequest(
        job_id=job_id,
        idempotency_key=idempotency_key,
        sender_slot_id=sender_slot_id,
        body=body,
        to_number=to_number,
        to_slot_id=to_slot_id,
    )


def execute_farm_sms_send(config: AgentConfig, request: FarmSmsSendRequest) -> FarmSmsSendResult:
    msisdn_map = load_slot_msisdn_map(config.slot_msisdn_map_path) if config.slot_msisdn_map_path else {}
    to_number = request.to_number
    if to_number is None and request.to_slot_id is not None:
        to_number = msisdn_map.get(int(request.to_slot_id))
        if not to_number:
            return FarmSmsSendResult(
                ok=False,
                http_status=400,
                job_id=request.job_id,
                idempotency_key=request.idempotency_key,
                status="failed",
                error=f"to_slot_id {request.to_slot_id} has no MSISDN mapping",
            )
    assert to_number is not None

    service = build_sms_dispatch_service(config)
    if service is None:
        return FarmSmsSendResult(
            ok=False,
            http_status=503,
            job_id=request.job_id,
            idempotency_key=request.idempotency_key,
            status="failed",
            error="SmsDispatchService not configured",
        )

    existing = service._outbox.get(request.idempotency_key)
    if existing is not None:
        logger.info(
            "duplicate_event_ignored idempotency_key=%s status=%s",
            request.idempotency_key,
            existing.final_status.value if existing.final_status else existing.status.value,
        )
        return FarmSmsSendResult(
            ok=True,
            http_status=200,
            job_id=existing.job_id,
            idempotency_key=request.idempotency_key,
            status=_map_final_status(existing.final_status),
            provider_message_id=existing.provider_message_id,
            duplicate=True,
        )

    logger.info(
        "provider_send_started job_id=%s sender_slot=%s dest=%s",
        request.job_id,
        request.sender_slot_id,
        redact_phone(to_number),
    )
    result = service.send_for_slot(
        request.sender_slot_id,
        to_number,
        request.body,
        idempotency_key=request.idempotency_key,
        job_id=request.job_id,
    )
    record = service._outbox.get(request.idempotency_key)
    final = record.final_status if record else None
    status = _map_final_status(final) if final else ("sent" if result.success else "failed")
    if result.error and "duplicate idempotency" in result.error:
        status = _map_final_status(final) if final else "accepted"
        return FarmSmsSendResult(
            ok=True,
            http_status=200,
            job_id=request.job_id,
            idempotency_key=request.idempotency_key,
            status=status,
            provider_message_id=result.provider_message_id,
            duplicate=True,
        )
    logger.info(
        "provider_send_result job_id=%s success=%s status=%s provider_id=%s",
        request.job_id,
        result.success,
        status,
        result.provider_message_id or "",
    )
    http_status = 200 if result.success else 422
    return FarmSmsSendResult(
        ok=result.success,
        http_status=http_status,
        job_id=request.job_id,
        idempotency_key=request.idempotency_key,
        status=status,
        provider_message_id=result.provider_message_id,
        error=result.error,
    )


def _parse_slot_id(raw: object) -> int:
    text = str(raw).strip()
    match = SLOT_ID_RE.match(text)
    if not match:
        raise ValueError("invalid sender_slot_id")
    slot = int(match.group(1) or match.group(2))
    if not 1 <= slot <= 20:
        raise ValueError("sender_slot_id out of range")
    return slot


def _map_final_status(final: SmsFinalStatus | None) -> str:
    if final is None:
        return "accepted"
    return final.value
