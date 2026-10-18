"""Canonical mapping to Supabase ``public.user_credits``.

Production balances live in Lovable/Supabase. The VPS never writes PostgREST
with a service-role key; it calls Lovable ``/billing/*`` which must update
this table by authenticated ``user_id``.
"""
from __future__ import annotations

from typing import Any

USER_CREDITS_TABLE = "user_credits"

# Live Supabase columns (authoritative).
COL_USER_ID = "user_id"
COL_AVAILABLE = "available_credits"
COL_TOTAL = "total_credits"
COL_TIER = "subscription_tier"
COL_WHMCS_SERVICE_ID = "whmcs_service_id"

# Legacy aliases accepted when reading older Lovable responses.
_LEGACY_AVAILABLE = ("credits_available", "available")
_LEGACY_TOTAL = ("credits_total", "total")
_LEGACY_TIER = ("tier", "package")


def credit_row_from_payload(raw: dict[str, Any] | None, *, user_id: str) -> dict[str, Any] | None:
    """Normalize a Lovable/Supabase credits row to live column names."""
    if not isinstance(raw, dict):
        return None
    available = _first_int(raw, COL_AVAILABLE, *_LEGACY_AVAILABLE)
    total = _first_int(raw, COL_TOTAL, *_LEGACY_TOTAL)
    if available is None or total is None or available < 0 or total < 0:
        return None
    tier = _first_text(raw, COL_TIER, *_LEGACY_TIER)
    service_id = _first_text(raw, COL_WHMCS_SERVICE_ID, "service_id", "whmcs_service")
    return {
        COL_USER_ID: user_id,
        COL_AVAILABLE: available,
        COL_TOTAL: total,
        COL_TIER: tier,
        COL_WHMCS_SERVICE_ID: service_id,
        "replayed": bool(raw.get("replayed")),
    }


def public_credits_body(
    *,
    user_id: str,
    available_credits: int,
    total_credits: int,
    subscription_tier: str | None,
    whmcs_service_id: str | None,
    replayed: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": True,
        "table": USER_CREDITS_TABLE,
        COL_USER_ID: user_id,
        COL_AVAILABLE: int(available_credits),
        COL_TOTAL: int(total_credits),
        COL_TIER: subscription_tier,
        COL_WHMCS_SERVICE_ID: whmcs_service_id,
        "replayed": bool(replayed),
    }
    if extra:
        body.update(extra)
    return body


def _first_int(raw: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key not in raw or raw.get(key) is None:
            continue
        value = raw.get(key)
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _first_text(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = str(raw.get(key) or "").strip()
        if text:
            return text
    return None
