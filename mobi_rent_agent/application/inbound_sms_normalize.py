"""Normalize VoidFix inbound webhook rows for Lovable delivery."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from domain.models import InboundSms
from infrastructure.slot_public_id import public_id_for_farm_slot


def provider_message_id_from_payload(payload: object, index: int) -> str | None:
    raw = _raw_message(payload, index)
    if raw and raw.get("ID") is not None:
        return str(raw.get("ID"))
    return None


def normalize_inbound_for_lovable(
    msg: InboundSms,
    *,
    farm_slot: int | None,
    provider_message_id: str | None,
    received_at: float | None = None,
) -> dict[str, Any]:
    ts = received_at if received_at is not None else _parse_received_at(msg.received_at)
    slot_uuid = public_id_for_farm_slot(int(farm_slot)) if farm_slot else None
    return {
        "slot_id": slot_uuid,
        "from": msg.from_number,
        "to": None,
        "body": msg.message,
        "received_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "provider_message_id": provider_message_id,
    }


def _raw_message(payload: object, index: int) -> dict[str, Any] | None:
    if isinstance(payload, list) and index < len(payload) and isinstance(payload[index], dict):
        return payload[index]
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list) and index < len(messages) and isinstance(messages[index], dict):
            return messages[index]
    return None


def _parse_received_at(value: str | None) -> float:
    if not value:
        return datetime.now(tz=timezone.utc).timestamp()
    try:
        normalized = value.replace("+0000", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return datetime.now(tz=timezone.utc).timestamp()
