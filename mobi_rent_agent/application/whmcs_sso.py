"""WHMCS dashboard SSO: HMAC over the raw Base64URL payload, one-time nonce.

React StrictMode mounts the redeem page twice. The same nonce + IP/User-Agent
pair may replay within 10 seconds and receive the original 200 instead of a
false 401. A different client fingerprint still gets 401 after first use.

The HMAC is computed over the Base64URL payload string as sent, never over
the decoded JSON.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from application.vps_api_contract import error_body

logger = logging.getLogger("vps_backend.whmcs_sso")

NONCE_REPLAY_SECONDS = 10.0
MAX_PAYLOAD_LEN = 4096
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")


@dataclass(frozen=True)
class WhmcsSsoResult:
    http_status: int
    body: dict[str, Any]


@dataclass
class _NonceRecord:
    fingerprint: str
    replay_until: float
    consumed_until: float
    body: dict[str, Any]


def b64url_decode(text: str) -> bytes:
    raw = (text or "").strip()
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad)


def sign_sso_payload(secret: str, payload_b64: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def verify_sso_signature(secret: str, payload_b64: str, provided: str | None) -> bool:
    if not secret or not payload_b64 or not provided:
        return False
    token = provided.strip()
    if token.lower().startswith("sha256="):
        token = token.split("=", 1)[1].strip()
    expected = sign_sso_payload(secret, payload_b64)
    if len(token) != len(expected):
        return False
    return hmac.compare_digest(expected, token)


def _fingerprint(ip: str | None, user_agent: str | None) -> str:
    material = f"{ip or ''}\n{user_agent or ''}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()


class WhmcsSsoService:
    def __init__(
        self,
        *,
        secret: str | None,
        replay_seconds: float = NONCE_REPLAY_SECONDS,
        clock: Callable[[], float] = time.time,
        tenant_store: Any = None,
    ) -> None:
        self._secret = (secret or "").strip()
        self._replay = float(replay_seconds)
        self._clock = clock
        self._tenant = tenant_store
        self._lock = threading.Lock()
        self._nonces: dict[str, _NonceRecord] = {}

    def redeem(
        self,
        *,
        payload_b64: str | None,
        signature: str | None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> WhmcsSsoResult:
        if not self._secret:
            return WhmcsSsoResult(503, error_body("auth_not_configured"))
        payload = (payload_b64 or "").strip()
        if not payload or len(payload) > MAX_PAYLOAD_LEN:
            return WhmcsSsoResult(401, error_body("unauthorized"))
        if not verify_sso_signature(self._secret, payload, signature):
            logger.info("whmcs_sso_bad_signature")
            return WhmcsSsoResult(401, error_body("unauthorized"))
        claims = self._decode_claims(payload)
        if claims is None:
            return WhmcsSsoResult(401, error_body("unauthorized"))
        nonce = str(claims.get("nonce") or "").strip()
        if not _NONCE_RE.match(nonce):
            return WhmcsSsoResult(401, error_body("unauthorized"))
        now = float(self._clock())
        exp = _optional_int(claims.get("exp"))
        fingerprint = _fingerprint(ip, user_agent)
        with self._lock:
            self._purge_locked(now)
            existing = self._nonces.get(nonce)
            if existing is not None:
                if existing.fingerprint == fingerprint and existing.replay_until > now:
                    body = dict(existing.body)
                    body["replayed"] = True
                    logger.info("whmcs_sso_nonce_replay")
                    return WhmcsSsoResult(200, body)
                logger.info("whmcs_sso_nonce_rejected")
                return WhmcsSsoResult(401, error_body("unauthorized"))
            if exp is not None and exp < now:
                return WhmcsSsoResult(401, error_body("unauthorized"))
            body = self._success_body(claims)
            consumed_until = float(exp) if exp is not None else now + 60.0
            self._nonces[nonce] = _NonceRecord(
                fingerprint=fingerprint,
                replay_until=now + self._replay,
                consumed_until=max(consumed_until, now + self._replay),
                body=dict(body),
            )
        logger.info("whmcs_sso_ok")
        return WhmcsSsoResult(200, body)

    def _decode_claims(self, payload_b64: str) -> dict[str, Any] | None:
        try:
            decoded = b64url_decode(payload_b64)
            raw = json.loads(decoded.decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def _success_body(self, claims: dict[str, Any]) -> dict[str, Any]:
        user_id = _optional_uuid(claims.get("user_id") or claims.get("supabase_user_id"))
        email = str(claims.get("email") or "").strip().lower()
        if email and not _EMAIL_RE.match(email):
            email = ""
        client_id = _optional_int(claims.get("client_id") or claims.get("whmcs_client_id"))
        if user_id and self._tenant is not None:
            ensure = getattr(self._tenant, "ensure_profile", None)
            if callable(ensure) and email:
                try:
                    ensure(user_id, email)
                except (TypeError, ValueError, OSError):
                    logger.warning("whmcs_sso_profile_failed")
        phone_count = _optional_int(claims.get("phone_count"))
        if phone_count is None or phone_count < 1:
            phone_count = 1
        if phone_count > 20:
            phone_count = 20
        return {
            "ok": True,
            "authenticated": True,
            "replayed": False,
            "user_id": user_id,
            "email": email or None,
            "whmcs_client_id": client_id,
            "phone_count": phone_count,
        }

    def _purge_locked(self, now: float) -> None:
        expired = [key for key, rec in self._nonces.items() if rec.consumed_until <= now]
        for key in expired:
            self._nonces.pop(key, None)


def _optional_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_uuid(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except (TypeError, ValueError):
        return None
