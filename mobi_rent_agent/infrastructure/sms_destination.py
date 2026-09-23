"""SMS destination validation (E.164)."""
from __future__ import annotations

import re

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def validate_sms_destination(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Allow common formatting; normalize to +digits
    compact = "+" + "".join(ch for ch in text if ch.isdigit())
    if not _E164_RE.match(compact):
        return None
    return compact


def idempotency_fingerprint(to_number: str, body: str) -> str:
    import hashlib

    basis = f"{to_number}\n{body}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
