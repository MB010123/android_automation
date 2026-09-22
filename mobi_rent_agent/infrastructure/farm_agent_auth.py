"""Shared-secret auth for VPS ↔ farm HTTP (constant-time compare)."""
from __future__ import annotations

import hmac


def extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def authorize_farm_request(provided: str | None, expected: str | None) -> bool:
    if not expected:
        return False
    if provided is None:
        return False
    return hmac.compare_digest(provided, expected)
