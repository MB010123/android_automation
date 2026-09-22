"""Parse and redact IMEI values. Never log a raw 15-digit IMEI."""
from __future__ import annotations

import re

_IMEI_RE = re.compile(r"^\d{15}$")


def is_valid_imei(value: str | None) -> bool:
    if not value or not _IMEI_RE.match(value):
        return False
    digits = [int(ch) for ch in value]
    checksum = 0
    for index, digit in enumerate(digits):
        if index % 2 == 1:
            doubled = digit * 2
            checksum += doubled // 10 + doubled % 10
        else:
            checksum += digit
    return checksum % 10 == 0


def redact_imei(value: str | None) -> str:
    if not value:
        return "<missing>"
    if len(value) < 8:
        return "<redacted>"
    return f"{value[:4]}…{value[-4:]}"


def parse_cmd_phone_imei(stdout: str) -> str | None:
    text = (stdout or "").strip()
    lower = text.lower()
    if "permission denied" in lower or "securityexception" in lower:
        raise PermissionError("ADB shell is not allowed to read IMEI on this Android build")
    match = re.search(r"\b(\d{15})\b", text)
    if match:
        return match.group(1)
    return None
