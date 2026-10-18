"""WHMCS webhook → public.user_credits mapping and billing config."""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.user_credits_contract import (
    COL_AVAILABLE,
    COL_TIER,
    COL_TOTAL,
    COL_WHMCS_SERVICE_ID,
    USER_CREDITS_TABLE,
)
from application.whmcs_billing_service import LovableCreditWallet, WhmcsBillingService
from application.whmcs_webhook import (
    IDENTIFIER_HEADER,
    SIGNATURE_HEADER,
    parse_whmcs_payload,
    sign_whmcs_body,
)
from application.vps_lovable_routes import parse_route
from infrastructure.credit_wallet_store import CreditWalletStore, InsufficientCreditsError
from infrastructure.lovable_server_client import LovableServerClient
from tests.test_supabase_auth import FakeHTTP, FakeResponse

USER = "11111111-1111-4111-8111-111111111111"
IDENT = "test-whmcs-identifier-not-production"
SECRET = "test-whmcs-secret-not-production-32b"
CART = "https://store.example.com/cart.php?a=add&pid=9"
STORE = "https://store.example.com"


def _cmd(event: str = "InvoicePaid", **extra) -> dict:
    body = {
        "event": event,
        "user_id": USER,
        "credit_delta": 2,
        "subscription_tier": "starter",
        "whmcs_service_id": "4401",
        "invoice_id": "8801",
        "idempotency_key": extra.pop("idempotency_key", "invoice-8801"),
    }
    body.update(extra)
    return body


def _service(tmp_path: Path, **kwargs) -> tuple[WhmcsBillingService, CreditWalletStore]:
    wallet = CreditWalletStore(tmp_path / "credits.sqlite")
    service = WhmcsBillingService(
        identifier=IDENT,
        secret=SECRET,
        wallet=wallet,
        store_url=STORE,
        cart_url=CART,
        **kwargs,
    )
    return service, wallet


def _signed(service: WhmcsBillingService, payload: dict, *, ident: str = IDENT, secret: str = SECRET):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return service.handle(
        raw,
        identifier=ident,
        signature=sign_whmcs_body(secret, raw),
    )


def test_parse_route_and_grant_revoke_events():
    assert parse_route("/api/webhooks/whmcs").kind == "whmcs_webhook"
    assert parse_route("/billing/config").kind == "billing_config"
    assert parse_route("/billing/credits").kind == "billing_credits"
    grant = parse_whmcs_payload(_cmd())
    assert grant.action == "credit_grant" and grant.user_id == USER
    assert grant.subscription_tier == "starter"
    assert grant.whmcs_service_id == "4401"
    revoke = parse_whmcs_payload(_cmd("CancellationRequest", credit_delta=2))
    assert revoke.action == "credit_revoke"
    assert parse_whmcs_payload({"event": "InvoicePaid"}) == "invalid_user_id"
    assert parse_whmcs_payload(_cmd(event="SomethingElse")) == "invalid_whmcs_event"


def test_invoice_paid_updates_user_credits_columns(tmp_path: Path):
    service, wallet = _service(tmp_path)
    result = _signed(service, _cmd())
    assert result.http_status == 200
    assert result.body["table"] == USER_CREDITS_TABLE
    assert result.body[COL_AVAILABLE] == 2
    assert result.body[COL_TOTAL] == 2
    assert result.body[COL_TIER] == "starter"
    assert result.body[COL_WHMCS_SERVICE_ID] == "4401"
    assert result.body["replayed"] is False
    snap = wallet.get(USER)
    assert snap is not None
    assert snap.available_credits == 2
    assert snap.subscription_tier == "starter"
    assert snap.whmcs_service_id == "4401"


def test_cancellation_decreases_available_credits(tmp_path: Path):
    service, wallet = _service(tmp_path)
    assert _signed(service, _cmd()).http_status == 200
    cancelled = _signed(
        service,
        _cmd("CancellationRequest", credit_delta=2, idempotency_key="cancel-8801"),
    )
    assert cancelled.http_status == 200
    assert cancelled.body[COL_AVAILABLE] == 0
    assert cancelled.body["subscription_status"] == "cancelled"
    assert wallet.get(USER).total_credits == 2


def test_activate_deduct_and_terminate_refund_are_atomic(tmp_path: Path):
    service, wallet = _service(tmp_path)
    assert _signed(service, _cmd(credit_delta=1)).http_status == 200
    rental = str(uuid4())
    activated = service.deduct_for_activation(user_id=USER, rental_id=rental)
    assert activated.http_status == 200
    assert activated.body[COL_AVAILABLE] == 0
    again = service.deduct_for_activation(user_id=USER, rental_id=rental)
    assert again.http_status == 200 and again.body["replayed"] is True
    assert wallet.get(USER).available_credits == 0
    blocked = service.deduct_for_activation(user_id=USER, rental_id=str(uuid4()))
    assert blocked.http_status == 409
    assert blocked.body["error"] == "insufficient_credits"
    refunded = service.refund_for_termination(user_id=USER, rental_id=rental)
    assert refunded.http_status == 200
    assert refunded.body[COL_AVAILABLE] == 1
    assert wallet.get(USER).total_credits == 1


def test_webhook_is_idempotent_and_rejects_bad_auth(tmp_path: Path):
    service, wallet = _service(tmp_path)
    first = _signed(service, _cmd())
    again = _signed(service, _cmd())
    assert first.body["replayed"] is False
    assert again.body["replayed"] is True
    assert wallet.get(USER).available_credits == 2
    raw = json.dumps(_cmd()).encode()
    denied = service.handle(raw, identifier=IDENT, signature="deadbeef")
    assert denied.http_status == 401
    assert denied.body["error"] == "unauthorized"
    wrong_id = service.handle(
        raw, identifier="other-identifier", signature=sign_whmcs_body(SECRET, raw)
    )
    assert wrong_id.http_status == 401
    off = WhmcsBillingService(identifier=None, secret=None, wallet=wallet)
    assert off.handle(raw, identifier=IDENT, signature="x").body["error"] == "webhook_not_configured"


def test_lovable_wallet_maps_user_credits_columns(tmp_path: Path):
    http = FakeHTTP(
        {
            ("POST", "https://lovable.example/api/vps/billing/whmcs-event"): FakeResponse(
                200,
                {
                    "ok": True,
                    "available_credits": 5,
                    "total_credits": 5,
                    "subscription_tier": "pro",
                    "whmcs_service_id": "99",
                    "subscription_status": "active",
                },
            )
        }
    )
    client = LovableServerClient(
        "https://lovable.example/api/vps",
        "vps-to-lovable-machine-token-32b-min",
        session=http,
    )
    service = WhmcsBillingService(
        identifier=IDENT,
        secret=SECRET,
        wallet=LovableCreditWallet(client),
    )
    result = _signed(service, _cmd(subscription_tier="pro", credit_delta=5, whmcs_service_id="99"))
    assert result.http_status == 200
    assert result.body[COL_AVAILABLE] == 5
    assert result.body[COL_TIER] == "pro"
    assert result.body[COL_WHMCS_SERVICE_ID] == "99"
    assert http.calls[-1][0] == "POST"
    assert http.calls[-1][1].endswith("/billing/whmcs-event")
    posted = http.calls[-1][2]
    assert posted["user_id"] == USER
    assert posted["table"] == USER_CREDITS_TABLE
    assert posted["action"] == "credit_grant"
    assert posted[COL_TIER] == "pro"
    assert posted[COL_WHMCS_SERVICE_ID] == "99"


def test_lovable_accepts_legacy_credit_aliases(tmp_path: Path):
    http = FakeHTTP(
        {
            ("POST", "https://lovable.example/api/vps/billing/whmcs-event"): FakeResponse(
                200,
                {
                    "ok": True,
                    "credits_available": 3,
                    "credits_total": 3,
                    "tier": "legacy",
                },
            )
        }
    )
    client = LovableServerClient(
        "https://lovable.example/api/vps",
        "vps-to-lovable-machine-token-32b-min",
        session=http,
    )
    service = WhmcsBillingService(
        identifier=IDENT,
        secret=SECRET,
        wallet=LovableCreditWallet(client),
    )
    result = _signed(service, _cmd(credit_delta=3, subscription_tier="legacy"))
    assert result.http_status == 200
    assert result.body[COL_AVAILABLE] == 3
    assert result.body[COL_TIER] == "legacy"


def test_billing_config_is_https_only(tmp_path: Path):
    service, _wallet = _service(tmp_path)
    body = service.billing_config().body
    assert body["upgrade_url"] == CART
    assert body["store_url"] == STORE
    assert body["credits_table"] == USER_CREDITS_TABLE
    insecure = WhmcsBillingService(
        identifier=IDENT,
        secret=SECRET,
        wallet=CreditWalletStore(tmp_path / "other.sqlite"),
        store_url="http://store.example.com",
        cart_url="/billing",
    )
    assert insecure.billing_config().body["upgrade_url"] is None
    assert insecure.billing_config().body["store_url"] is None


def test_local_deduct_raises_when_empty(tmp_path: Path):
    wallet = CreditWalletStore(tmp_path / "empty.sqlite")
    try:
        wallet.deduct(user_id=USER, amount=1, idempotency_key="x")
        raise AssertionError("expected InsufficientCreditsError")
    except InsufficientCreditsError:
        pass


def _http(method: str, url: str, *, headers: dict | None = None, body: dict | None = None):
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw else {})


def test_http_webhook_and_billing_config(tmp_path: Path):
    import importlib.util

    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server_whmcs", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    Handler = mod.Handler
    wallet = CreditWalletStore(tmp_path / "http-credits.sqlite")
    Handler.whmcs_billing_service = WhmcsBillingService(
        identifier=IDENT,
        secret=SECRET,
        wallet=wallet,
        store_url=STORE,
        cart_url=CART,
    )
    Handler.farm_management_service = None
    Handler.auth_service = None
    Handler.allowed_origins = frozenset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, cfg = _http("GET", f"{base}/billing/config")
        assert status == 200
        assert cfg["upgrade_url"] == CART
        assert cfg["credits_table"] == USER_CREDITS_TABLE
        payload = _cmd(idempotency_key=str(uuid4()))
        raw = json.dumps(payload, separators=(",", ":")).encode()
        status, body = _http(
            "POST",
            f"{base}/api/webhooks/whmcs",
            headers={
                IDENTIFIER_HEADER: IDENT,
                SIGNATURE_HEADER: sign_whmcs_body(SECRET, raw),
            },
            body=payload,
        )
        assert status == 200, body
        assert body[COL_AVAILABLE] == 2
        assert body[COL_WHMCS_SERVICE_ID] == "4401"
        status, denied = _http(
            "POST",
            f"{base}/api/webhooks/whmcs",
            headers={"Authorization": "Bearer user-jwt"},
            body=payload,
        )
        assert status == 401
    finally:
        server.shutdown()
        wallet.close()
