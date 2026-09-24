"""Stable VPS API response shapes and safe error messages (no secrets)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.vps_job_store import VpsJobRecord

ERROR_MESSAGES: dict[str, str] = {
    "slot_not_found": "Farm bay or public slot ID is not configured",
    "slot_unavailable": "Bay is assigned or has a pending job",
    "device_offline": "Farm reports the bay ADB device is offline",
    "invalid_rental_id": "rental_id must be a UUID",
    "invalid_request": "Required assignment fields are missing or invalid",
    "invalid_json": "Request body must be JSON object",
    "job_not_found": "Unknown job_id",
    "unsupported_action": "Action is not in the allowlist",
    "rate_limited": "Too many requests for this bay",
    "farm_unreachable": "Farm Agent is unreachable or not configured",
    "provisioning_failed": "Provisioning did not complete on the Farm Agent",
    "action_not_supported": "Farm Agent does not support this operation",
    "unauthorized": "Missing or invalid FARM_SERVICE_TOKEN",
    "idempotency_conflict": "Idempotency key reused with different payload",
    "missing_idempotency_key": "idempotency_key is required",
    "invalid_destination": "SMS destination number is invalid",
    "invalid_message": "SMS body is missing or too long",
    "not_found": "Resource not found",
}


def iso_ts(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


TEMPORARY_FAILURE_ERRORS = frozenset(
    {"farm_unreachable", "farm_down", "device_offline", "device_busy", "timeout", "farm_timeout"}
)
UNSUPPORTED_ERRORS = frozenset({"action_not_supported", "agent_not_configured"})
MANUAL_ACTION_MARKERS = ("human", "lpa", "settings", "manual")


def failure_class(record: VpsJobRecord) -> str | None:
    """Classify a failed job: unsupported | requires_manual_action | temporary | permanent."""
    if record.status != "failed":
        return None
    error = (record.error or "").strip().lower()
    message = ""
    if isinstance(record.result_payload, dict):
        message = str(record.result_payload.get("message") or "").lower()
    if error in UNSUPPORTED_ERRORS:
        return "unsupported"
    combined = f"{error} {message}"
    if any(marker in combined for marker in MANUAL_ACTION_MARKERS):
        return "requires_manual_action"
    if error in TEMPORARY_FAILURE_ERRORS or error.startswith("http_5"):
        return "temporary"
    return "permanent"


def provisioning_phase(record: VpsJobRecord) -> str | None:
    if record.type != "assign":
        return None
    if record.status == "pending":
        return "queued"
    if record.status == "running":
        return "provisioning"
    if record.status == "done":
        return "completed"
    if record.status == "failed":
        klass = failure_class(record)
        if klass in ("unsupported", "requires_manual_action"):
            return klass
        return "failed"
    return "unknown"


def job_response_body(record: VpsJobRecord) -> dict[str, Any]:
    state = record.status
    body: dict[str, Any] = {
        "ok": True,
        "job_id": record.job_id,
        "type": record.type,
        "state": state,
        "status": state,
        "progress": record.progress,
        "bay": record.farm_slot_id,
        "error": record.error,
        "message": ERROR_MESSAGES.get(record.error or "", record.error) if record.error else None,
        "created_at": iso_ts(record.created_at),
        "started_at": iso_ts(record.started_at),
        "completed_at": iso_ts(record.completed_at),
        "updated_at": iso_ts(record.updated_at),
    }
    if record.farm_slot_id is not None:
        body["slot_id"] = public_id_for_farm_slot(int(record.farm_slot_id))
    phase = provisioning_phase(record)
    if phase is not None:
        body["provisioning_phase"] = phase
    klass = failure_class(record)
    if klass is not None:
        body["failure_class"] = klass
    if record.result_payload:
        safe = {k: v for k, v in record.result_payload.items() if k not in ("activation_code", "qr_url")}
        if safe:
            body["result"] = safe
    return body


def assign_acceptance_body(*, job_id: str, bay: int, status: str = "pending") -> dict[str, Any]:
    return {
        "ok": True,
        "job_id": job_id,
        "bay": bay,
        "slot_id": public_id_for_farm_slot(bay),
        "status": status,
    }


def action_acceptance_body(*, job_id: str, bay: int, action: str, status: str = "pending") -> dict[str, Any]:
    return {
        "ok": True,
        "job_id": job_id,
        "bay": bay,
        "slot_id": public_id_for_farm_slot(bay),
        "action": action,
        "status": status,
    }


def error_body(code: str, *, message: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error": code,
        "message": message or ERROR_MESSAGES.get(code, code),
    }
