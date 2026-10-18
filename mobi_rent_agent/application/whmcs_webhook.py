"""Parse and authenticate WHMCS billing webhooks from vps_sync.php.

The browser never calls this path. Identifier and HMAC secret come from
environment configuration only — never from a request-body role flag.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

IDENTIFIER_HEADER = "X-WHMCS-Identifier"
SIGNATURE_HEADER = "X-WHMCS-Signature"
GRANT_EVENTS = frozenset({"invoicepaid", "afterorderaccept"})
REVOKE_EVENTS = frozenset(
    {
        "servicesuspensionaudit",
        "cancellationrequest",
        "aftersuspend",
        "servicecancelled",
    }
)
ACTION_GRANT = "credit_grant"
ACTION_REVOKE = "credit_revoke"
_TIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_MAX_CREDITS = 1000


@dataclass(frozen=True)
class WhmcsWebhookCommand:
    event: str
    action: str
    user_id: str
    credit_delta: int
    subscription_tier: str | None
    subscription_status: str
    whmcs_client_id: int | None
    invoice_id: str | None
    whmcs_service_id: str | None
    idempotency_key: str

    @property
    def tier(self) -> str | None:
        return self.subscription_tier

    @property
    def service_id(self) -> str | None:
        return self.whmcs_service_id


def sign_whmcs_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_whmcs_signature(secret: str, body: bytes, provided: str | None) -> bool:
    if not secret or not provided:
        return False
    token = provided.strip()
    if token.lower().startswith("sha256="):
        token = token.split("=", 1)[1].strip()
    expected = sign_whmcs_body(secret, body)
    return hmac.compare_digest(expected, token)


def secure_equals(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(
        hashlib.sha256(left.encode("utf-8")).digest(),
        hashlib.sha256(right.encode("utf-8")).digest(),
    )


def parse_whmcs_payload(raw: Any) -> WhmcsWebhookCommand | str:
    """Return a command or a stable error code. Never invents a user UUID."""
    if not isinstance(raw, dict):
        return "invalid_request"
    event = str(raw.get("event") or "").strip()
    normalized = event.lower()
    if normalized in GRANT_EVENTS:
        action = ACTION_GRANT
        status = "active"
    elif normalized in REVOKE_EVENTS:
        action = ACTION_REVOKE
        status = "cancelled" if "cancel" in normalized else "suspended"
    else:
        return "invalid_whmcs_event"
    user_id = _parse_user_id(raw.get("user_id") or raw.get("supabase_user_id"))
    if user_id is None:
        return "invalid_user_id"
    delta = _parse_credit_delta(raw, required=action == ACTION_GRANT)
    if delta is None:
        return "invalid_credit_delta"
    tier_raw = raw.get("subscription_tier")
    if tier_raw in (None, ""):
        tier_raw = raw.get("tier")
    if tier_raw in (None, ""):
        tier_raw = raw.get("package")
    tier = _parse_tier(tier_raw)
    if tier_raw not in (None, "") and tier is None:
        return "invalid_tier"
    client_id = _optional_int(raw.get("whmcs_client_id") or raw.get("client_id"))
    invoice_id = _optional_text(raw.get("invoice_id"))
    service_id = _optional_text(
        raw.get("whmcs_service_id") or raw.get("service_id")
    )
    idem = _optional_text(raw.get("idempotency_key")) or _default_idempotency(
        event=normalized, user_id=user_id, invoice_id=invoice_id, service_id=service_id
    )
    return WhmcsWebhookCommand(
        event=event,
        action=action,
        user_id=user_id,
        credit_delta=delta,
        subscription_tier=tier,
        subscription_status=status,
        whmcs_client_id=client_id,
        invoice_id=invoice_id,
        whmcs_service_id=service_id,
        idempotency_key=idem,
    )


def parse_whmcs_json_body(body: bytes) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_user_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except (TypeError, ValueError):
        return None


def _parse_credit_delta(raw: dict[str, Any], *, required: bool) -> int | None:
    value = raw.get("credit_delta")
    if value is None:
        value = raw.get("credits")
    if value is None:
        value = raw.get("package_credits")
    if value is None:
        return 0 if not required else None
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > _MAX_CREDITS:
        return None
    if required and number < 1:
        return None
    return number


def _parse_tier(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if _TIER_RE.match(text) else None


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _default_idempotency(
    *, event: str, user_id: str, invoice_id: str | None, service_id: str | None
) -> str:
    parts = [event, user_id, invoice_id or "", service_id or ""]
    return "whmcs-" + "-".join(parts)
