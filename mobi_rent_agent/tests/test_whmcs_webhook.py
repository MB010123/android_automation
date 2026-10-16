"""WHMCS webhook parser, auth, credit mapping, and billing config."""
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

from application.whmcs_billing_service import LovableCreditWallet, WhmcsBillingService
from application.whmcs_webhook import (
    IDENTIFIER_HEADER,
    SIGNATURE_HEADER,
    parse_whmcs_payload,
    sign_whmcs_body,
)
from application.vps_lovable_routes import parse_route
from infrastructure.credit_wallet_store import CreditWalletStore
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
        "tier": "starter",
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
    grant = parse_whmcs_payload(_cmd())
    assert grant.action == "credit_grant" and grant.user_id == USER
    revoke = parse_whmcs_payload(_cmd("CancellationRequest", credit_delta=2))
    assert revoke.action == "credit_revoke"
    assert parse_whmcs_payload({"event": "InvoicePaid"}) == "invalid_user_id"
    assert parse_whmcs_payload(_cmd(event="SomethingElse")) == "invalid_whmcs_event"


def test_invoice_paid_increments_credits_and_tier(tmp_path: Path):
    service, wallet = _service(tmp_path)
    result = _signed(service, _cmd())
    assert result.http_status == 200
    assert result.body["credits_available"] == 2
    assert result.body["credits_total"] == 2
    assert result.body["tier"] == "starter"
    assert result.body["subscription_status"] == "active"
    assert result.body["replayed"] is False
    snap = wallet.get(USER)
    assert snap is not None and snap.credits_available == 2


def test_cancellation_decreases_credits_and_flags_status(tmp_path: Path):
    service, wallet = _service(tmp_path)
    assert _signed(service, _cmd()).http_status == 200
    cancelled = _signed(
        service,
        _cmd("CancellationRequest", credit_delta=2, idempotency_key="cancel-8801"),
    )
    assert cancelled.http_status == 200
    assert cancelled.body["credits_available"] == 0
    assert cancelled.body["subscription_status"] == "cancelled"
    assert wallet.get(USER).credits_total == 2


def test_webhook_is_idempotent_and_rejects_bad_auth(tmp_path: Path):
    service, wallet = _service(tmp_path)
    first = _signed(service, _cmd())
    again = _signed(service, _cmd())
    assert first.body["replayed"] is False
    assert again.body["replayed"] is True
    assert wallet.get(USER).credits_available == 2
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


def test_lovable_wallet_maps_user_uuid(tmp_path: Path):
    http = FakeHTTP(
        {
            ("POST", "https://lovable.example/api/vps/billing/whmcs-event"): FakeResponse(
                200,
                {
                    "ok": True,
                    "credits_available": 5,
                    "credits_total": 5,
                    "tier": "pro",
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
    result = _signed(service, _cmd(tier="pro", credit_delta=5))
    assert result.http_status == 200
    assert result.body["credits_available"] == 5
    assert result.body["tier"] == "pro"
    assert http.calls[-1][0] == "POST"
    assert http.calls[-1][1].endswith("/billing/whmcs-event")
    posted = http.calls[-1][2]
    assert posted["user_id"] == USER
    assert posted["action"] == "credit_grant"


def test_billing_config_is_https_only(tmp_path: Path):
    service, _wallet = _service(tmp_path)
    body = service.billing_config().body
    assert body["upgrade_url"] == CART
    assert body["store_url"] == STORE
    insecure = WhmcsBillingService(
        identifier=IDENT,
        secret=SECRET,
        wallet=CreditWalletStore(tmp_path / "other.sqlite"),
        store_url="http://store.example.com",
        cart_url="/billing",
    )
    assert insecure.billing_config().body["upgrade_url"] is None
    assert insecure.billing_config().body["store_url"] is None


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
        assert body["credits_available"] == 2
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
