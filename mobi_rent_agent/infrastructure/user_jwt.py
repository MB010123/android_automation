"""HS256 JWT for user access tokens. Claims are identity only — never passwords or farm secrets."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_user_jwt(
    *,
    user_id: str,
    session_id: str,
    secret: str,
    ttl_seconds: int,
    now: float | None = None,
) -> str:
    issued = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": user_id,
                "type": "user",
                "iat": issued,
                "exp": issued + int(ttl_seconds),
                "jti": session_id,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def verify_user_jwt(token: str, secret: str, *, now: float | None = None) -> dict[str, Any] | None:
    if not token or token.count(".") != 2:
        return None
    header_b64, payload_b64, sig_b64 = token.split(".")
    signing = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).digest()
    try:
        provided = _b64url_decode(sig_b64)
    except (ValueError, OSError):
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "user":
        return None
    ts = int(now if now is not None else time.time())
    try:
        exp = int(payload["exp"])
    except (KeyError, TypeError, ValueError):
        return None
    if ts >= exp:
        return None
    return payload


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
