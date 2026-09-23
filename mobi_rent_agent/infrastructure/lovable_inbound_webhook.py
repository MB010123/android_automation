"""VPS → Lovable inbound SMS webhook client (HMAC signed)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import requests

logger = logging.getLogger("vps_backend.lovable_webhook")

SIGNATURE_HEADER = "X-Mobi-Rent-Signature"


def sign_payload(secret: str, body_bytes: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body_bytes: bytes, provided: str | None) -> bool:
    if not secret or not provided:
        return False
    expected = sign_payload(secret, body_bytes)
    return hmac.compare_digest(expected, provided.strip())


def deliver_inbound_to_lovable(
    url: str,
    secret: str,
    payload: dict[str, Any],
    *,
    timeout: float = 10.0,
    session: requests.Session | None = None,
) -> bool:
    """POST normalized inbound payload. Returns True on 2xx."""
    if not url or not secret:
        return False
    body_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = sign_payload(secret, body_bytes)
    client = session or requests.Session()
    try:
        response = client.post(
            url,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                SIGNATURE_HEADER: signature,
            },
            timeout=timeout,
        )
    except requests.RequestException:
        logger.warning("lovable_inbound_delivery_failed")
        return False
    if 200 <= response.status_code < 300:
        logger.info("lovable_inbound_delivery_ok http=%s", response.status_code)
        return True
    logger.warning("lovable_inbound_delivery_failed http=%s", response.status_code)
    return False
