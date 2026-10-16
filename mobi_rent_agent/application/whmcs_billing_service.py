"""Apply authenticated WHMCS billing events to the credit wallet.

The VPS verifies identifier + HMAC, then updates credits/tier. When a Lovable
client is configured, the authoritative write is POST /billing/whmcs-event.
Otherwise the local wallet store is used (tests / isolated VPS).
"""
from __future__ import annotations

import logging
from typing import Any

from application.vps_api_contract import error_body
from application.whmcs_webhook import (
    IDENTIFIER_HEADER,
    SIGNATURE_HEADER,
    WhmcsWebhookCommand,
    parse_whmcs_json_body,
    parse_whmcs_payload,
    secure_equals,
    verify_whmcs_signature,
)
from infrastructure.credit_wallet_store import CreditWalletStore, WalletSnapshot

logger = logging.getLogger("vps_backend.whmcs")


class ApiResult:
    def __init__(self, http_status: int, body: dict[str, Any]) -> None:
        self.http_status = http_status
        self.body = body


class LovableCreditWallet:
    """Forwards a verified WHMCS command to Lovable/Supabase."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def apply(self, command: WhmcsWebhookCommand) -> WalletSnapshot | str:
        result = self._client.request(
            "POST",
            "/billing/whmcs-event",
            json_body={
                "event": command.event,
                "action": command.action,
                "user_id": command.user_id,
                "credit_delta": command.credit_delta,
                "tier": command.tier,
                "subscription_status": command.subscription_status,
                "whmcs_client_id": command.whmcs_client_id,
                "invoice_id": command.invoice_id,
                "service_id": command.service_id,
                "idempotency_key": command.idempotency_key,
            },
            idempotency_key=command.idempotency_key,
        )
        if result.error == "not_found":
            return "user_not_found"
        if result.error in {"unavailable", "unauthorized", "rate_limited"}:
            return "tenant_unavailable"
        if result.error and result.http_status >= 400 and result.error != "conflict":
            return "tenant_unavailable"
        body = result.body if isinstance(result.body, dict) else {}
        try:
            available = int(body.get("credits_available"))
            total = int(body.get("credits_total"))
        except (TypeError, ValueError):
            return "tenant_unavailable"
        if available < 0 or total < 0:
            return "tenant_unavailable"
        return WalletSnapshot(
            user_id=command.user_id,
            credits_available=available,
            credits_total=total,
            tier=str(body.get("tier")).strip() or command.tier,
            subscription_status=str(body.get("subscription_status") or command.subscription_status),
            replayed=bool(body.get("replayed")),
        )


class WhmcsBillingService:
    def __init__(
        self,
        *,
        identifier: str | None,
        secret: str | None,
        wallet: CreditWalletStore | LovableCreditWallet,
        store_url: str | None = None,
        cart_url: str | None = None,
    ) -> None:
        self._identifier = (identifier or "").strip() or None
        self._secret = (secret or "").strip() or None
        self._wallet = wallet
        self.store_url = _public_https_url(store_url)
        self.cart_url = _public_https_url(cart_url)

    @property
    def configured(self) -> bool:
        return bool(self._identifier and self._secret)

    def billing_config(self) -> ApiResult:
        return ApiResult(
            200,
            {
                "ok": True,
                "upgrade_url": self.cart_url,
                "store_url": self.store_url,
            },
        )

    def handle(
        self,
        body: bytes,
        *,
        identifier: str | None,
        signature: str | None,
    ) -> ApiResult:
        if not self.configured:
            return ApiResult(503, error_body("webhook_not_configured"))
        if not secure_equals(identifier, self._identifier) or not verify_whmcs_signature(
            self._secret or "", body, signature
        ):
            logger.info("whmcs_webhook_unauthorized")
            return ApiResult(401, error_body("unauthorized"))
        payload = parse_whmcs_json_body(body)
        parsed = parse_whmcs_payload(payload)
        if isinstance(parsed, str):
            return ApiResult(400, error_body(parsed if parsed != "invalid_request" else "invalid_json"))
        logger.info(
            "whmcs_webhook_accepted event=%s action=%s user=%s credits=%s",
            parsed.event,
            parsed.action,
            parsed.user_id[:8],
            parsed.credit_delta,
        )
        applied = self._apply(parsed)
        if isinstance(applied, str):
            status = 404 if applied == "user_not_found" else 502
            return ApiResult(status, error_body(applied))
        return ApiResult(
            200,
            {
                "ok": True,
                "event": parsed.event,
                "action": parsed.action,
                "user_id": parsed.user_id,
                "credits_available": applied.credits_available,
                "credits_total": applied.credits_total,
                "tier": applied.tier,
                "subscription_status": applied.subscription_status,
                "replayed": applied.replayed,
            },
        )

    def _apply(self, command: WhmcsWebhookCommand) -> WalletSnapshot | str:
        if isinstance(self._wallet, LovableCreditWallet):
            return self._wallet.apply(command)
        return self._wallet.apply(
            user_id=command.user_id,
            action=command.action,
            credit_delta=command.credit_delta,
            tier=command.tier,
            subscription_status=command.subscription_status,
            idempotency_key=command.idempotency_key,
        )


def _public_https_url(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not text.startswith("https://"):
        return None
    return text


# Imported by OpenAPI/docs; keep header names in one place.
WHMCS_IDENTIFIER_HEADER = IDENTIFIER_HEADER
WHMCS_SIGNATURE_HEADER = SIGNATURE_HEADER
