"""Stable VPS API response shapes and safe error messages (no secrets)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.vps_job_store import VpsJobRecord

ERROR_MESSAGES: dict[str, str] = {
    "slot_not_found": "Farm bay or public slot ID is not configured",
        "slot_unavailable": "Bay is reserved, assigned, or has a pending job",
    "rental_active": "Rental is still active and has not ended",
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
    "timeout": "The operation timed out",
    "unauthorized": "Missing or invalid credentials",
    "idempotency_conflict": "Idempotency key reused with different payload",
    "missing_idempotency_key": "idempotency_key is required",
    "invalid_destination": "SMS destination number is invalid",
    "invalid_message": "SMS body is missing or too long",
    "not_found": "Resource not found",
    "auth_not_hosted_on_vps": (
        "User sign-up and login are hosted by Lovable Cloud Auth, not this VPS"
    ),
    "invalid_credentials": "Email or password is incorrect",
    "invalid_email": "email must be a valid address",
    "weak_password": "Password does not meet production requirements",
    "auth_not_configured": "User authentication is not configured on this VPS",
    "auth_unavailable": "Authentication service is temporarily unavailable",
    "esim_ref_unavailable": "Authoritative eSIM reference is missing from Lovable slot data",
    "forbidden": "Not authorized for this rental, slot, or device",
    "rental_not_found": "No rental exists for this id",
    "rental_not_owned": "This rental does not belong to the signed-in customer",
    "phone_offline": "The assigned phone is offline",
    "phone_unavailable": "The assigned phone is not available for remote access",
    "remote_access_not_ready": "Remote access is not active for this rental",
    "session_expired": "The rental or remote-access session has expired",
    "gads_unavailable": "Remote screen service is temporarily unavailable",
    "manual_esim_required": "eSIM must be activated manually in Android Settings",
    "invalid_control": "The remote-control request is missing or has invalid fields",
    "remote_access_not_configured": "Remote-access POC is disabled or the platform is not configured",
    "remote_access_platform_error": "Remote-access platform rejected the request",
    "remote_access_busy": "Another rental currently holds remote access on this phone",
    "forbidden_control": "This remote-control action is not allowed",
    "setup_state_blocked": "Stay on the Android eSIM setup screens",
    "phone_not_ready": "This control is available after the phone is ready",
    "setup_incomplete": "Phone setup is not complete yet",
    "remote_access_not_found": "No remote-access session exists for this rental",
    "qr_upload_missing": "Request must be multipart/form-data with a qr_image file",
    "qr_not_an_image": "QR payload is not a PNG, JPG, or WEBP image",
    "qr_image_too_large": "QR image exceeds the maximum allowed size",
    "qr_zero_byte": "Remote QR file size is zero",
    "qr_not_on_device": "Remote QR file is missing after ADB push",
    "qr_push_failed": "ADB push of QR image failed",
    "qr_download_failed": "QR image could not be transferred to the Farm Agent",
}


def iso_ts(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


TEMPORARY_FAILURE_ERRORS = frozenset(
    {"farm_unreachable", "farm_down", "device_offline", "device_busy", "timeout", "farm_timeout"}
)
UNSUPPORTED_ERRORS = frozenset({"action_not_supported", "agent_not_configured"})
# Include the Farm Agent's honest unattended-download refusal. That outcome is
# "customer must use Android Settings/LPA", not a broken bay.
MANUAL_ACTION_MARKERS = (
    "human",
    "lpa",
    "settings",
    "manual",
    "esim authority",
)


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
        safe = {
            k: v
            for k, v in record.result_payload.items()
            if k not in ("activation_code", "qr_url", "esim_qr_url", "image_base64")
        }
        if safe:
            body["result"] = safe
    if isinstance(record.result_payload, dict):
        install_state = record.result_payload.get("install_state")
        if install_state:
            body["install_state"] = install_state
        if "activation_code_sent" in record.result_payload:
            body["activation_code_sent"] = bool(record.result_payload.get("activation_code_sent"))
        if record.result_payload.get("tenant_record_error"):
            body["tenant_record_error"] = True
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


def customer_known_unknown_field(value: Any, field: str) -> dict[str, Any]:
    """Passthrough known/unknown pair. Empty or whitespace is null + unknown."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return {field: None, f"{field}_status": "unknown"}
    return {field: text, f"{field}_status": "known"}


def customer_eid_fields(eid: str | None) -> dict[str, Any]:
    """IMEI2-style known/unknown for a stored EID. Never invents a placeholder."""
    return customer_known_unknown_field(eid, "eid")


def customer_inventory_identity_fields(
    *,
    eid: str | None = None,
    imei2: str | None = None,
    carrier: str | None = None,
    phone_number: str | None = None,
) -> dict[str, Any]:
    """Owned-slot inventory for customer device-status. Never fabricates radio."""
    return {
        **customer_known_unknown_field(imei2, "imei2"),
        **customer_eid_fields(eid),
        **customer_known_unknown_field(carrier, "carrier"),
        **customer_known_unknown_field(phone_number, "phone_number"),
        "cellular_status": "unknown",
    }
