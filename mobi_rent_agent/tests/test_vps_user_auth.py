"""Supabase-authoritative user auth, ownership, CORS, and credential separation."""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.auth_service import AuthService, validate_esim_storage_key
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from application.vps_slot_sms_service import VpsSlotSmsService
from infrastructure.auth_rate_limiter import AuthRateLimiter
from infrastructure.farm_sms_client import FarmDispatchResponse
from infrastructure.outbound_message_store import OutboundMessageStore
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.slot_status_store import SlotStatusStore
from infrastructure.supabase_jwt import sign_supabase_access_token, verify_supabase_access_token
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryGoTrue, MemoryTenant, seed_owned_slot

SLOT1 = public_id_for_farm_slot(1)
SLOT2 = public_id_for_farm_slot(2)
STRONG = "correct-horse-battery"
JWT_SECRET = "unit-test-supabase-jwt-secret-not-for-production"
FARM_TOKEN = "farm-service-secret"
AGENT_TOKEN = "farm-agent-secret"
WEBHOOK_SECRET = "voidfix-webhook-secret"


class FarmStub:
    def __call__(self) -> dict:
        return {"ok": True, "offline_slots": [], "mapped_slots": [1, 2], "slot_count": 2, "adb_online": 2}


class SyncWorker(VpsJobWorker):
    def enqueue_process(self, job_id: str) -> None:
        return None


class MockFarmSms:
    def send_sms(self, **kwargs):
        return FarmDispatchResponse(ok=True, http_status=200, body={"ok": True}, error=None)


def _service(gotrue: MemoryGoTrue | None = None, tenant: MemoryTenant | None = None) -> AuthService:
    return AuthService(supabase=gotrue or MemoryGoTrue(), tenant=tenant or MemoryTenant())


def test_signup_login_duplicate_and_validation():
    svc = _service()
    created = svc.signup({"email": "A@Example.COM", "password": STRONG})
    assert created.http_status == 200
    assert created.body["user"]["email"] == "a@example.com"
    raw = json.dumps(created.body)
    assert STRONG not in raw
    assert "password_hash" not in raw
    assert created.body["session"]["token_type"] == "Bearer"
    assert created.access_token and created.access_token.startswith("sb-")
    dup = svc.signup({"email": "a@example.com", "password": STRONG})
    assert dup.http_status == 400
    assert dup.body["error"] == "invalid_request"
    assert svc.signup({"email": "not-an-email", "password": STRONG}).body["error"] == "invalid_email"
    assert svc.signup({"email": "ok@example.com", "password": "short"}).body["error"] == "weak_password"
    ok = svc.login({"email": "a@example.com", "password": STRONG})
    assert ok.http_status == 200
    bad = svc.login({"email": "a@example.com", "password": "wrong-password-xx"})
    assert bad.http_status == 401
    assert bad.body["error"] == "invalid_credentials"
    missing = svc.login({"email": "nobody@example.com", "password": STRONG})
    assert missing.http_status == 401
    assert missing.body["error"] == "invalid_credentials"


def test_logout_revokes_supabase_token():
    svc = _service()
    created = svc.signup({"email": "lock@example.com", "password": STRONG})
    token = created.access_token
    ctx = svc.validate_user_token(token)
    assert ctx is not None
    assert svc.logout(ctx).http_status == 200
    assert svc.validate_user_token(token) is None


def test_supabase_jwt_rejects_expired_and_non_user_role():
    token = sign_supabase_access_token(
        user_id=str(uuid.uuid4()),
        email="jwt@example.com",
        secret=JWT_SECRET,
        ttl_seconds=10,
        now=1_000.0,
    )
    assert verify_supabase_access_token(token, JWT_SECRET, now=1_000.0) is not None
    assert verify_supabase_access_token(token, JWT_SECRET, now=1_020.0) is None
    assert verify_supabase_access_token(FARM_TOKEN, JWT_SECRET) is None
    svc = AuthService(supabase=MemoryGoTrue(), jwt_secret=JWT_SECRET)
    assert svc.validate_user_token(token) is None
    live = sign_supabase_access_token(
        user_id="user-1",
        email="jwt@example.com",
        secret=JWT_SECRET,
        ttl_seconds=3600,
        issuer="https://example.supabase.co",
    )
    ctx = svc.validate_user_token(live)
    assert ctx is not None
    assert ctx.user is not None
    assert ctx.user.user_id == "user-1"
    assert ctx.user.password_hash == ""


def test_forgot_password_is_generic():
    svc = _service()
    assert svc.forgot_password({"email": "exp@example.com"}).body == {"ok": True}
    assert svc.forgot_password({"email": "missing@example.com"}).body == {"ok": True}


def test_sql_injection_email_is_rejected():
    svc = _service()
    payload = {"email": "a@example.com'; DROP TABLE users; --", "password": STRONG}
    result = svc.signup(payload)
    assert result.http_status == 400


def test_esim_storage_key_rejects_arbitrary_urls():
    assert validate_esim_storage_key("users/abc/esim.png") == "users/abc/esim.png"
    assert validate_esim_storage_key("https://evil.example/qr.png") is None
    assert (
        validate_esim_storage_key(
            "https://files.loanerphones.com/private/a",
            allowed_url_prefixes=("https://files.loanerphones.com/",),
        )
        == "https://files.loanerphones.com/private/a"
    )


def _load_mod():
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server_user_auth", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _http(method: str, url: str, *, token: str | None = None, body: dict | None = None, origin: str | None = None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        parsed = json.loads(raw) if raw else {}
        return exc.code, parsed, dict(exc.headers)


def _start_auth_server(tmp_path: Path, *, rate_limit: int = 50):
    mod = _load_mod()
    Handler = mod.Handler
    gotrue = MemoryGoTrue()
    tenant = MemoryTenant()
    auth = AuthService(supabase=gotrue, tenant=tenant)
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    status = SlotStatusStore(tmp_path / "status.sqlite")
    worker = SyncWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=None,
        poll_interval_seconds=3600.0,
        auth_store=tenant,
    )
    farm_svc = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=FarmStub(),
        known_farm_slots={1, 2},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        status_store=status,
        auth_store=tenant,
        esim_url_prefixes=("https://example.test/",),
    )
    messages = OutboundMessageStore(tmp_path / "sms.sqlite")
    sms = VpsSlotSmsService(
        message_store=messages,
        farm_client=MockFarmSms(),
        farm_status_fetcher=FarmStub(),
        known_farm_slots={1, 2},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        tenant_store=tenant,
    )
    Handler.farm_service_token = FARM_TOKEN
    Handler.auth_service = auth
    Handler.auth_rate_limiter = AuthRateLimiter(limit=rate_limit, window_seconds=60.0, max_keys=64)
    Handler.farm_management_service = farm_svc
    Handler.slot_sms_service = sms
    Handler.allowed_origins = frozenset({"https://app.loanerphones.com"})
    Handler.auth_cookie_name = None
    Handler.webhook_secret = WEBHOOK_SECRET
    Handler.tenant_store = tenant
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], tenant, auth


def test_http_signup_login_me_logout_and_no_password_logs(tmp_path: Path, caplog):
    caplog.set_level(logging.DEBUG)
    server, port, _tenant, _auth = _start_auth_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger("vps_backend.auth").addHandler(handler)
        status, body, _ = _http(
            "POST",
            f"{base}/auth/signup",
            body={"email": "one@example.com", "password": STRONG, "role": "admin"},
        )
        logging.getLogger("vps_backend.auth").removeHandler(handler)
        assert status == 200
        assert body["user"]["email"] == "one@example.com"
        assert "role" not in body["user"] or body["user"].get("role") != "admin"
        token = body["session"]["access_token"]
        assert STRONG not in json.dumps(body)
        assert STRONG not in stream.getvalue()
        status, me, _ = _http("GET", f"{base}/auth/me", token=token)
        assert status == 200
        assert me["user"]["id"] == body["user"]["id"]
        assert me["user"]["role"] == "user"
        status, sess, _ = _http("GET", f"{base}/auth/session", token=token)
        assert status == 200
        assert sess["token_type"] == "USER_ACCESS_TOKEN"
        status, out, _ = _http("POST", f"{base}/auth/logout", token=token, body={})
        assert status == 200
        status, denied, _ = _http("GET", f"{base}/auth/me", token=token)
        assert status == 401
        logs = caplog.text.lower()
        assert STRONG.lower() not in logs
    finally:
        server.shutdown()


def test_http_credential_separation(tmp_path: Path):
    server, port, _tenant, _auth = _start_auth_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        _, created, _ = _http("POST", f"{base}/auth/signup", body={"email": "sep@example.com", "password": STRONG})
        user_token = created["session"]["access_token"]
        status, farm_sess, _ = _http("GET", f"{base}/auth/session", token=FARM_TOKEN)
        assert status == 200
        assert farm_sess["audience"] == "farm_service"
        status, body, _ = _http("GET", f"{base}/auth/me", token=FARM_TOKEN)
        assert status == 401
        status, body, _ = _http("GET", f"{base}/farm/slots/available", token=user_token)
        assert status == 401
        status, body, _ = _http("GET", f"{base}/farm/slots/available", token=FARM_TOKEN)
        assert status == 200
        status, body, _ = _http("GET", f"{base}/auth/me", token=AGENT_TOKEN)
        assert status == 401
        status, body, _ = _http("GET", f"{base}/slots", token=WEBHOOK_SECRET)
        assert status == 401
        status, body, _ = _http("GET", f"{base}/slots", token=user_token)
        assert status == 200
        assert body["slots"] == []
    finally:
        server.shutdown()


def test_http_ownership_and_esim(tmp_path: Path):
    server, port, tenant, _auth = _start_auth_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        _, a, _ = _http("POST", f"{base}/auth/signup", body={"email": "a@example.com", "password": STRONG})
        _, b, _ = _http("POST", f"{base}/auth/signup", body={"email": "b@example.com", "password": STRONG})
        token_a = a["session"]["access_token"]
        token_b = b["session"]["access_token"]
        user_a = a["user"]["id"]
        rental = str(uuid.uuid4())
        user_esim = {
            "rental_id": rental,
            "qr_code_url": "users/a/esim/qr",
            "carrier": "T-Mobile",
            "user_id": b["user"]["id"],
        }
        status, unowned, _ = _http("POST", f"{base}/slots/{SLOT1}/esim", token=token_a, body=user_esim)
        assert status == 404
        assert unowned.get("error") == "slot_not_found"
        assert tenant.owner_of_slot(1) is None
        status, listed, _ = _http("GET", f"{base}/slots", token=token_a)
        assert listed["count"] == 0

        seed_owned_slot(
            tenant,
            bay=1,
            rental_id=rental,
            user_id=user_a,
            qr_code_url="https://example.test/private/qr",
            carrier_name="T-Mobile",
        )
        status, farm_job, _ = _http(
            "POST",
            f"{base}/farm/slots/1/assign",
            token=FARM_TOKEN,
            body={
                "rental_id": rental,
                "esim_qr_url": "https://evil.example/override",
                "carrier": "client-carrier",
                "user_id": user_a,
            },
        )
        assert status == 202
        assert farm_job["ok"] is True
        assert tenant.owner_of_slot(1) == user_a

        status, listed, _ = _http("GET", f"{base}/slots", token=token_a)
        assert listed["count"] == 1
        assert listed["slots"][0]["slot_id"] == SLOT1
        status, other, _ = _http("GET", f"{base}/slots", token=token_b)
        assert other["count"] == 0

        status, job, _ = _http("POST", f"{base}/slots/{SLOT1}/esim", token=token_a, body=user_esim)
        assert status == 202
        assert job["ok"] is True
        status, stolen, _ = _http("POST", f"{base}/slots/{SLOT1}/esim", token=token_b, body=user_esim)
        assert status == 404
        assert stolen.get("error") == "slot_not_found"
        status, slot2, _ = _http(
            "POST",
            f"{base}/slots/{SLOT2}/esim",
            token=token_a,
            body={
                "rental_id": str(uuid.uuid4()),
                "qr_code_url": "users/a/esim/other",
                "carrier": "T-Mobile",
            },
        )
        assert status == 404
        assert tenant.owner_of_slot(2) is None

        status, detail, _ = _http("GET", f"{base}/slots/{SLOT1}", token=token_a)
        assert status == 200
        assert "proxy_auth" not in detail
        assert "gateway_api_key" not in detail
        status, hidden, _ = _http("GET", f"{base}/slots/{SLOT1}", token=token_b)
        assert status == 404
        status, hidden, _ = _http("GET", f"{base}/slots/{SLOT1}/status", token=token_b)
        assert status == 404
        status, hidden, _ = _http("GET", f"{base}/slots/{SLOT1}/messages", token=token_b)
        assert status == 404
        status, hidden, _ = _http(
            "POST",
            f"{base}/slots/{SLOT1}/sms/send",
            token=token_b,
            body={"to": "+15555550100", "body": "hi", "idempotency_key": "k1"},
        )
        assert status == 404
        status, hidden, _ = _http("POST", f"{base}/slots/{SLOT1}/actions/reboot", token=token_b, body={})
        assert status == 404
        status, _reboot, _ = _http("POST", f"{base}/slots/{SLOT1}/actions/reboot", token=token_a, body={})
        assert status in {202, 409, 503}
        status, farm_all, _ = _http("GET", f"{base}/slots", token=FARM_TOKEN)
        assert farm_all["count"] == 2
    finally:
        server.shutdown()


def test_farm_assign_unknown_user_is_rejected(tmp_path: Path):
    server, port, tenant, _auth = _start_auth_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        status, body, _ = _http(
            "POST",
            f"{base}/farm/slots/1/assign",
            token=FARM_TOKEN,
            body={
                "rental_id": str(uuid.uuid4()),
                "esim_qr_url": "https://example.test/private/qr",
                "carrier": "T-Mobile",
                "user_id": str(uuid.uuid4()),
            },
        )
        assert status == 404
        assert body["error"] == "slot_not_found"
        assert tenant.owner_of_slot(1) is None
    finally:
        server.shutdown()


def test_http_signup_without_auth_service_is_not_configured(tmp_path: Path):
    server, port, _tenant, _auth = _start_auth_server(tmp_path)
    mod_handler = server.RequestHandlerClass
    previous = mod_handler.auth_service
    mod_handler.auth_service = None
    base = f"http://127.0.0.1:{port}"
    try:
        status, body, _ = _http(
            "POST",
            f"{base}/auth/signup",
            body={"email": "none@example.com", "password": STRONG},
        )
        assert status == 503
        assert body["error"] == "auth_not_configured"
        assert STRONG not in json.dumps(body)
    finally:
        mod_handler.auth_service = previous
        server.shutdown()


def test_http_cors_and_rate_limit_and_malformed(tmp_path: Path):
    server, port, _tenant, _auth = _start_auth_server(tmp_path, rate_limit=3)
    base = f"http://127.0.0.1:{port}"
    try:
        req = urllib.request.Request(
            f"{base}/auth/login",
            method="OPTIONS",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"
        req = urllib.request.Request(
            f"{base}/auth/login",
            method="OPTIONS",
            headers={"Origin": "https://evil.example"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") is None
        status, body, _ = _http("POST", f"{base}/auth/login", body={"email": "x@example.com", "password": STRONG})
        assert status == 401
        req = urllib.request.Request(
            f"{base}/auth/login",
            data=b"{not-json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 400
        huge = json.dumps({"email": "z@example.com", "password": "x" * 70000}).encode()
        req = urllib.request.Request(
            f"{base}/auth/signup",
            data=huge,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 400
        for _ in range(3):
            _http("POST", f"{base}/auth/login", body={"email": "brute@example.com", "password": STRONG})
        status, limited, _ = _http(
            "POST",
            f"{base}/auth/login",
            body={"email": "brute@example.com", "password": STRONG},
        )
        assert status == 429
        assert limited["error"] == "rate_limited"
    finally:
        server.shutdown()


def test_auth_rate_limiter_is_bounded():
    limiter = AuthRateLimiter(limit=2, window_seconds=60.0, max_keys=16)
    for i in range(40):
        limiter.check(f"k{i}")
    assert len(limiter._hits) <= 16
