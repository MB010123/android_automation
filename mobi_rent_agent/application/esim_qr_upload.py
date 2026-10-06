"""Parse customer QR image uploads and shape public VPS responses.

The browser sends bytes to the VPS. The VPS forwards them to Farm Agent.
This module never talks to ADB, GADS, or the Farm token.
"""
from __future__ import annotations

import re
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
_BOUNDARY_RE = re.compile(r'boundary\s*=\s*(?:"([^"]+)"|([^\s;]+))', re.IGNORECASE)
_NAME_RE = re.compile(r'name=(?:"([^"]*)"|([^\s;]+))', re.IGNORECASE)


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
    """Return `qr_image` file bytes, or None if the part is missing.

    Reads the selected file bytes from multipart/form-data. Does not treat a
    JSON body, storage path, or filename-only field as an image.
    """
    if not content_type or "multipart/form-data" not in content_type.lower():
        return None
    if body is None:
        return None
    boundary = _multipart_boundary(content_type)
    if boundary:
        parsed = _parse_qr_image_by_boundary(boundary, body)
        if parsed is not None:
            return parsed
    return _parse_qr_image_email(content_type, body)


def _multipart_boundary(content_type: str) -> str | None:
    match = _BOUNDARY_RE.search(content_type)
    if not match:
        return None
    value = match.group(1) or match.group(2) or ""
    return value.strip() or None


def _header_name(headers: bytes) -> str | None:
    text = headers.decode("latin-1", "replace")
    for line in text.split("\r\n"):
        if line.lower().startswith("content-disposition:"):
            match = _NAME_RE.search(line)
            if match:
                return (match.group(1) or match.group(2) or "").strip()
    return None


def _decode_part_body(headers: bytes, payload: bytes) -> bytes:
    text = headers.decode("latin-1", "replace").lower()
    if "content-transfer-encoding: base64" in text:
        import base64

        try:
            return base64.b64decode(payload, validate=False)
        except (ValueError, TypeError):
            return payload
    if "content-transfer-encoding: quoted-printable" in text:
        import quopri

        return quopri.decodestring(payload)
    return payload


def _parse_qr_image_by_boundary(boundary: str, body: bytes) -> bytes | None:
    marker = b"--" + boundary.encode("latin-1", "replace")
    parts = body.split(marker)
    found = False
    payload: bytes | None = None
    for raw in parts:
        chunk = raw
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if chunk in {b"", b"--", b"--\r\n", b"--\n"} or chunk.startswith(b"--"):
            continue
        header_end = chunk.find(b"\r\n\r\n")
        sep_len = 4
        if header_end < 0:
            header_end = chunk.find(b"\n\n")
            sep_len = 2
        if header_end < 0:
            continue
        headers = chunk[:header_end]
        data = chunk[header_end + sep_len :]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        elif data.endswith(b"\n"):
            data = data[:-1]
        if _header_name(headers) != QR_IMAGE_FIELD:
            continue
        found = True
        payload = _decode_part_body(headers, data)
    if not found:
        return None
    return payload if payload is not None else b""


def _parse_qr_image_email(content_type: str, body: bytes) -> bytes | None:
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
