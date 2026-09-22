"""Redaction helpers for farm logs. Never log secrets or full identifiers."""
from __future__ import annotations


def redact_serial(value: str | None) -> str:
    if value is None or not str(value).strip():
        return "<missing>"
    serial = str(value).strip()
    if len(serial) < 8:
        return "****"
    return f"{serial[:4]}****{serial[-4:]}"


def redact_phone(value: str | None) -> str:
    if value is None:
        return "<missing>"
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 4:
        return "+XXX****"
    return f"+XXX****{digits[-4:]}"


def redact_secret(text: str, secret: str | None) -> str:
    if not text or not secret:
        return text
    return text.replace(secret, "<redacted>")


def normalize_msisdn(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())
