"""Apply authenticated WHMCS billing events to Supabase ``user_credits``.

The VPS verifies identifier + HMAC, then updates credits/tier. When a Lovable
client is configured, the authoritative write is POST /billing/whmcs-event
(which must mutate ``public.user_credits``). Otherwise the local wallet store
mirrors the same column names for tests.
"""
from __future__ import annotations

import logging
from typing import Any

from application.user_credits_contract import (
    COL_AVAILABLE,
    COL_TIER,
    COL_TOTAL,
    COL_USER_ID,
    COL_WHMCS_SERVICE_ID,
    USER_CREDITS_TABLE,
    credit_row_from_payload,
    public_credits_body,
)
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
from infrastructure.credit_wallet_store import (
    ACTION_DEDUCT,
    ACTION_REFUND,
    CreditWalletStore,
    InsufficientCreditsError,
    WalletSnapshot,
)

logger = logging.getLogger("vps_backend.whmcs")


class ApiResult:
    def __init__(self, http_status: int, body: dict[str, Any]) -> None:
        self.http_status = http_status
        self.body = body


class LovableCreditWallet:
    """Forwards verified billing mutations to Lovable → ``user_credits``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, user_id: str) -> WalletSnapshot | str:
        result = self._client.request("GET", f"/billing/credits/{user_id}")
        if result.error == "not_found":
            return "user_not_found"
        if result.error:
            return "tenant_unavailable"
        body = result.body if isinstance(result.body, dict) else {}
        row = body.get("credits") if isinstance(body.get("credits"), dict) else body
        normalized = credit_row_from_payload(row if isinstance(row, dict) else None, user_id=user_id)
        if normalized is None:
            return "tenant_unavailable"
        return _snapshot_from_row(normalized)

    def apply(self, command: WhmcsWebhookCommand) -> WalletSnapshot | str:
        result = self._client.request(
            "POST",
            "/billing/whmcs-event",
            json_body={
                "table": USER_CREDITS_TABLE,
                "event": command.event,
                "action": command.action,
                "user_id": command.user_id,
                "credit_delta": command.credit_delta,
                COL_TIER: command.subscription_tier,
                "subscription_status": command.subscription_status,
                "whmcs_client_id": command.whmcs_client_id,
                "invoice_id": command.invoice_id,
                COL_WHMCS_SERVICE_ID: command.whmcs_service_id,
                "idempotency_key": command.idempotency_key,
            },
            idempotency_key=command.idempotency_key,
        )
        return self._parse_mutation_result(result, command.user_id)

    def deduct(
        self,
        *,
        user_id: str,
        amount: int = 1,
        idempotency_key: str,
        rental_id: str | None = None,
    ) -> WalletSnapshot | str:
        result = self._client.request(
            "POST",
            "/billing/credits/deduct",
            json_body={
                "table": USER_CREDITS_TABLE,
                "user_id": user_id,
                "amount": int(amount),
                "reason": "phone_activation",
                "rental_id": rental_id,
                "idempotency_key": idempotency_key,
            },
            idempotency_key=idempotency_key,
        )
        if result.error == "conflict" or (
            isinstance(result.body, dict) and result.body.get("error") == "insufficient_credits"
        ):
            return "insufficient_credits"
        return self._parse_mutation_result(result, user_id)

    def refund(
        self,
        *,
        user_id: str,
        amount: int = 1,
        idempotency_key: str,
        rental_id: str | None = None,
    ) -> WalletSnapshot | str:
        result = self._client.request(
            "POST",
            "/billing/credits/refund",
            json_body={
                "table": USER_CREDITS_TABLE,
                "user_id": user_id,
                "amount": int(amount),
                "reason": "phone_termination",
                "rental_id": rental_id,
                "idempotency_key": idempotency_key,
            },
            idempotency_key=idempotency_key,
        )
        return self._parse_mutation_result(result, user_id)

    def _parse_mutation_result(self, result: Any, user_id: str) -> WalletSnapshot | str:
        if result.error == "not_found":
            return "user_not_found"
        if result.error in {"unavailable", "unauthorized", "rate_limited"}:
            return "tenant_unavailable"
        if result.error and result.http_status >= 400 and result.error != "conflict":
            return "tenant_unavailable"
        body = result.body if isinstance(result.body, dict) else {}
        row = body.get("credits") if isinstance(body.get("credits"), dict) else body
        normalized = credit_row_from_payload(row if isinstance(row, dict) else None, user_id=user_id)
        if normalized is None:
            return "tenant_unavailable"
        return _snapshot_from_row(normalized)


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

    @property
    def wallet(self) -> CreditWalletStore | LovableCreditWallet:
        return self._wallet

    def billing_config(self) -> ApiResult:
        return ApiResult(
            200,
            {
                "ok": True,
                "upgrade_url": self.cart_url,
                "store_url": self.store_url,
                "credits_table": USER_CREDITS_TABLE,
            },
        )

    def get_user_credits(self, user_id: str) -> ApiResult:
        uid = str(user_id or "").strip()
        if not uid:
            return ApiResult(400, error_body("invalid_user_id"))
        if isinstance(self._wallet, LovableCreditWallet):
            got = self._wallet.get(uid)
        else:
            snap = self._wallet.get(uid)
            got = snap or WalletSnapshot(
                user_id=uid,
                available_credits=0,
                total_credits=0,
                subscription_tier=None,
                whmcs_service_id=None,
            )
        if isinstance(got, str):
            status = 404 if got == "user_not_found" else 502
            return ApiResult(status, error_body(got))
        return ApiResult(200, _public_body(got))

    def deduct_for_activation(
        self,
        *,
        user_id: str,
        rental_id: str,
        amount: int = 1,
    ) -> ApiResult:
        key = f"phone-activate-{rental_id}"
        try:
            if isinstance(self._wallet, LovableCreditWallet):
                applied = self._wallet.deduct(
                    user_id=user_id, amount=amount, idempotency_key=key, rental_id=rental_id
                )
            else:
                applied = self._wallet.deduct(
                    user_id=user_id, amount=amount, idempotency_key=key
                )
        except InsufficientCreditsError:
            return ApiResult(409, error_body("insufficient_credits"))
        if isinstance(applied, str):
            if applied == "insufficient_credits":
                return ApiResult(409, error_body("insufficient_credits"))
            status = 404 if applied == "user_not_found" else 502
            return ApiResult(status, error_body(applied))
        return ApiResult(
            200,
            _public_body(applied, extra={"action": ACTION_DEDUCT, "rental_id": rental_id}),
        )

    def refund_for_termination(
        self,
        *,
        user_id: str,
        rental_id: str,
        amount: int = 1,
    ) -> ApiResult:
        key = f"phone-terminate-{rental_id}"
        if isinstance(self._wallet, LovableCreditWallet):
            applied = self._wallet.refund(
                user_id=user_id, amount=amount, idempotency_key=key, rental_id=rental_id
            )
        else:
            applied = self._wallet.refund(
                user_id=user_id, amount=amount, idempotency_key=key
            )
        if isinstance(applied, str):
            status = 404 if applied == "user_not_found" else 502
            return ApiResult(status, error_body(applied))
        return ApiResult(
            200,
            _public_body(applied, extra={"action": ACTION_REFUND, "rental_id": rental_id}),
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
            "whmcs_webhook_accepted event=%s action=%s user=%s credits=%s table=%s",
            parsed.event,
            parsed.action,
            parsed.user_id[:8],
            parsed.credit_delta,
            USER_CREDITS_TABLE,
        )
        applied = self._apply(parsed)
        if isinstance(applied, str):
            status = 404 if applied == "user_not_found" else 502
            return ApiResult(status, error_body(applied))
        return ApiResult(
            200,
            _public_body(
                applied,
                extra={
                    "event": parsed.event,
                    "action": parsed.action,
                    "subscription_status": applied.subscription_status,
                },
            ),
        )

    def _apply(self, command: WhmcsWebhookCommand) -> WalletSnapshot | str:
        if isinstance(self._wallet, LovableCreditWallet):
            return self._wallet.apply(command)
        return self._wallet.apply(
            user_id=command.user_id,
            action=command.action,
            credit_delta=command.credit_delta,
            subscription_tier=command.subscription_tier,
            subscription_status=command.subscription_status,
            whmcs_service_id=command.whmcs_service_id,
            idempotency_key=command.idempotency_key,
        )


def _snapshot_from_row(row: dict[str, Any]) -> WalletSnapshot:
    return WalletSnapshot(
        user_id=str(row.get(COL_USER_ID) or ""),
        available_credits=int(row[COL_AVAILABLE]),
        total_credits=int(row[COL_TOTAL]),
        subscription_tier=row.get(COL_TIER),
        whmcs_service_id=row.get(COL_WHMCS_SERVICE_ID),
        subscription_status=str(row.get("subscription_status") or "active"),
        replayed=bool(row.get("replayed")),
    )


def _public_body(snap: WalletSnapshot, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return public_credits_body(
        user_id=snap.user_id,
        available_credits=snap.available_credits,
        total_credits=snap.total_credits,
        subscription_tier=snap.subscription_tier,
        whmcs_service_id=snap.whmcs_service_id,
        replayed=snap.replayed,
        extra=extra,
    )


def _public_https_url(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not text.startswith("https://"):
        return None
    return text


WHMCS_IDENTIFIER_HEADER = IDENTIFIER_HEADER
WHMCS_SIGNATURE_HEADER = SIGNATURE_HEADER
