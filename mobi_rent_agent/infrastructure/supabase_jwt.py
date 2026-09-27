"""Validate Supabase access tokens locally (HS256) without issuing a VPS JWT."""
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


def sign_supabase_access_token(
    *,
    user_id: str,
    email: str,
    secret: str,
    ttl_seconds: int = 3600,
    now: float | None = None,
    issuer: str | None = None,
) -> str:
    """Test helper: sign a Supabase-shaped access token. Not used in production."""
    issued = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "role": "authenticated",
        "aud": "authenticated",
        "iat": issued,
        "exp": issued + int(ttl_seconds),
    }
    if issuer:
        claims["iss"] = issuer
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def verify_supabase_access_token(
    token: str,
    secret: str,
    *,
    now: float | None = None,
    issuer: str | None = None,
) -> dict[str, Any] | None:
    """Return claims when the token is a valid authenticated-user Supabase JWT."""
    if not token or not secret or token.count(".") != 2:
        return None
    header_b64, payload_b64, sig_b64 = token.split(".")
    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        return None
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
    except (ValueError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("role") != "authenticated":
        return None
    if payload.get("aud") not in {None, "authenticated"}:
        return None
    if issuer and payload.get("iss") not in {issuer, f"{issuer}/auth/v1"}:
        return None
    ts = int(now if now is not None else time.time())
    try:
        exp = int(payload["exp"])
        sub = str(payload["sub"] or "").strip()
    except (KeyError, TypeError, ValueError):
        return None
    if not sub or ts >= exp:
        return None
    return payload
