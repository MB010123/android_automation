"""Parse customer QR image uploads and shape public VPS responses.

The browser sends bytes to the VPS. The VPS forwards them to Farm Agent.
This module never talks to ADB, GADS, or the Farm token.
"""
from __future__ import annotations

from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

from application.remote_access_farm_task import MAX_QR_IMAGE_BYTES
from application.vps_api_contract import ERROR_MESSAGES

QR_IMAGE_FIELD = "qr_image"
QR_UPLOAD_STEP = "qr_upload"
# Multipart envelope around the 5 MiB image cap.
MAX_UPLOAD_BODY_BYTES = MAX_QR_IMAGE_BYTES + 512 * 1024
_PUBLIC_SUCCESS_KEYS = (
    "ok",
    "placed",
    "job_id",
    "destination",
    "downloaded_size",
    "remote_size",
)


def qr_upload_error_body(error_code: str, *, message: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "step": QR_UPLOAD_STEP,
        "error_code": error_code,
        "message": message or ERROR_MESSAGES.get(error_code, error_code),
    }


def public_placement_success(details: dict[str, Any] | None, *, job_id: str) -> dict[str, Any]:
    """Customer-visible success. Omits serial, URLs, and Farm credentials."""
    source = details if isinstance(details, dict) else {}
    body: dict[str, Any] = {"ok": True, "placed": True, "job_id": job_id}
    for key in _PUBLIC_SUCCESS_KEYS:
        if key in {"ok", "placed", "job_id"}:
            continue
        if key in source:
            body[key] = source[key]
    return body


def parse_qr_image_multipart(content_type: str | None, body: bytes) -> bytes | None:
    """Return `qr_image` file bytes, or None if the part is missing."""
    if not content_type or "multipart/form-data" not in content_type.lower():
        return None
    header = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=HTTP).parsebytes(header + body)
    if not message.is_multipart():
        return None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name != QR_IMAGE_FIELD:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            return b""
        if isinstance(payload, str):
            return payload.encode("latin-1")
        return bytes(payload)
    return None
