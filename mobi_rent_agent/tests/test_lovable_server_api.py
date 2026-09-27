"""VPS → Lovable machine API: auth, isolation, IMEI2, messages, assignment."""
from __future__ import annotations

import json
import logging
import sys
import uuid
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.auth_service import AuthService
from application.vps_slot_sms_service import VpsSlotSmsService
from infrastructure.farm_sms_client import FarmDispatchResponse
from infrastructure.lovable_server_client import (
    MIN_TOKEN_LENGTH,
    LovableServerClient,
    lovable_client_from_env,
)
from infrastructure.lovable_tenant_store import (
    LovableTenantStore,
    UnconfiguredTenantStore,
    tenant_store_from_env,
)
from infrastructure.outbound_message_store import OutboundMessageRecord, OutboundMessageStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.supabase_gateway import SupabaseGateway, gotrue_from_env
from infrastructure.vps_openapi_spec import build_vps_openapi_document
from tests.fakes_supabase import MemoryGoTrue, MemoryTenant
from tests.test_blocker_fixes import _assign_body, _farm_svc
from tests.test_supabase_auth import FakeHTTP, FakeResponse
from tests.test_vps_user_auth import FARM_TOKEN, STRONG, _http, _start_auth_server

SLOT1 = public_id_for_farm_slot(1)
MACHINE_TOKEN = "vps-to-lovable-machine-token-32b-min"
IMEI2 = "353456789012345"
USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
BASE = "https://lovable.example/api/vps"


def _slot(user_id: str | None = USER_A, imei2: str | None = IMEI2, carrier: str = "Verizon") -> dict:
    return {
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "user_id": user_id,
        "motherboard_slot_num": 1,
        "carrier_name": carrier,
        "imei2": imei2,
        "phone_number": None,
    }


class _FarmOk:
    def send_sms(self, **kwargs):
        return FarmDispatchResponse(ok=True, http_status=200, body={"ok": True}, error=None)


def test_from_env_requires_dedicated_machine_token(monkeypatch):
    monkeypatch.setenv("LOVABLE_API_URL", BASE)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-must-not-be-used")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-must-not-be-used")
    monkeypatch.delenv("VPS_TO_LOVABLE_API_TOKEN", raising=False)
    assert lovable_client_from_env() is None
    assert isinstance(tenant_store_from_env(), UnconfiguredTenantStore)
    assert tenant_store_from_env().privileged is False

    monkeypatch.setenv("VPS_TO_LOVABLE_API_TOKEN", "too-short")
    assert lovable_client_from_env() is None

    monkeypatch.setenv("VPS_TO_LOVABLE_API_TOKEN", MACHINE_TOKEN)
    client = lovable_client_from_env()
    assert client is not None
    assert len(MACHINE_TOKEN) >= MIN_TOKEN_LENGTH


def test_gotrue_from_env_never_loads_service_role(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-test-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-must-stay-unused")
    gw = gotrue_from_env()
    assert gw is not None
    assert gw.has_service_role is False
    assert gw.rest_select("slots", query="select=id") is None


def test_valid_machine_token_reads_authoritative_slot():
    headers_seen: list[dict[str, str]] = []

    class HeaderHTTP(FakeHTTP):
        def request(self, method, url, json=None, headers=None, timeout=None):
            headers_seen.append(dict(headers or {}))
            return super().request(method, url, json=json, headers=headers, timeout=timeout)

    http = HeaderHTTP(
        {
            ("GET", f"{BASE}/slots/by-bay/1"): FakeResponse(200, {"slot": _slot()}),
            ("GET", f"{BASE}/slots"): FakeResponse(200, {"slots": [_slot()]}),
        }
    )
    store = LovableTenantStore(LovableServerClient(BASE, MACHINE_TOKEN, session=http))
    row = store.get_slot_row(1)
    assert row["imei2"] == IMEI2
    assert row["carrier_name"] == "Verizon"
    assert store.owner_of_slot(1) == USER_A
    owned = store.list_owned_slots(USER_A)
    assert [item.farm_slot_id for item in owned] == [1]
    assert headers_seen[0]["Authorization"] == f"Bearer {MACHINE_TOKEN}"


def test_invalid_and_missing_machine_token_rejected():
    http = FakeHTTP({("GET", f"{BASE}/slots/by-bay/1"): FakeResponse(401, {"error": "unauthorized"})})
    store = LovableTenantStore(LovableServerClient(BASE, MACHINE_TOKEN, session=http))
    with pytest.raises(OSError):
        store.get_slot_row(1)
    with pytest.raises(ValueError):
        LovableServerClient(BASE, "short")


def test_machine_token_never_in_response_or_logs():
    http = FakeHTTP({("GET", f"{BASE}/slots/by-bay/1"): FakeResponse(200, {"slot": _slot()})})
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("vps_backend.lovable_api")
    logger.addHandler(handler)
    try:
        client = LovableServerClient(BASE, MACHINE_TOKEN, session=http)
        result = client.request("GET", "/slots/by-bay/1")
    finally:
        logger.removeHandler(handler)
    assert result.error is None
    raw = stream.getvalue() + json.dumps(result.body)
    assert MACHINE_TOKEN not in raw
    assert "Authorization" not in raw


def test_user_token_is_not_machine_token():
    gotrue = MemoryGoTrue()
    svc = AuthService(supabase=gotrue, tenant=MemoryTenant())
    created = svc.signup({"email": "user@example.com", "password": STRONG})
    assert created.access_token
    assert created.access_token != MACHINE_TOKEN
    assert created.access_token != FARM_TOKEN
    assert created.body["session"]["token_type"] == "Bearer"


def test_client_cannot_override_imei2_or_user_id_on_claim():
    captured: list[dict[str, Any] | None] = []

    class CaptureHTTP(FakeHTTP):
        def request(self, method, url, json=None, headers=None, timeout=None):
            captured.append(json)
            return super().request(method, url, json=json, headers=headers, timeout=timeout)

    http = CaptureHTTP(
        {
            ("POST", f"{BASE}/slots/by-bay/1/claim"): FakeResponse(200, {"slot": _slot()}),
        }
    )
    store = LovableTenantStore(LovableServerClient(BASE, MACHINE_TOKEN, session=http))
    assert store.claim_slot(1, USER_A, str(uuid.uuid4())) is True
    body = captured[0]
    assert body is not None
    assert "imei2" not in body
    assert set(body) <= {"user_id", "rental_id"}
    assert body["user_id"] == USER_A


def test_messages_come_from_lovable_not_sqlite(tmp_path: Path):
    http = FakeHTTP(
        {
            ("GET", f"{BASE}/messages/msg-owned"): FakeResponse(
                200,
                {
                    "message": {
                        "message_id": "msg-owned",
                        "slot_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "direction": "outbound",
                        "phone_number": "+15555550100",
                        "message_body": "hello",
                        "status": "queued",
                    }
                },
            ),
            ("GET", f"{BASE}/slots/by-bay/1"): FakeResponse(200, {"slot": _slot()}),
            ("GET", f"{BASE}/slots/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"): FakeResponse(
                200, {"slot": _slot()}
            ),
        }
    )
    tenant = LovableTenantStore(LovableServerClient(BASE, MACHINE_TOKEN, session=http))
    store = OutboundMessageStore(tmp_path / "api.sqlite")
    store.insert(
        OutboundMessageRecord(
            message_id="sqlite-only",
            slot_public_id=SLOT1,
            farm_slot_id=1,
            direction="out",
            to_number="+15555550100",
            body="queue-only",
            status="queued",
            error_code=None,
            idempotency_key="k-sqlite",
            content_fingerprint="fp",
            provider_message_id=None,
            created_at=1.0,
            updated_at=1.0,
            dispatch_attempts=0,
        )
    )
    sms = VpsSlotSmsService(
        message_store=store,
        farm_client=_FarmOk(),
        farm_status_fetcher=lambda: {"ok": True, "offline_slots": []},
        known_farm_slots={1, 2},
        tenant_store=tenant,
    )
    own = sms.get_tenant_message("msg-owned", user_id=USER_A)
    assert own.http_status == 200
    assert own.body["body"] == "hello"
    other = sms.get_tenant_message("msg-owned", user_id=USER_B)
    assert other.http_status == 404
    sqlite_only = sms.get_tenant_message("sqlite-only", user_id=USER_A)
    assert sqlite_only.http_status == 404


def test_farm_assign_claims_before_job_and_conflict_creates_none(tmp_path: Path):
    tenant = MemoryTenant()
    tenant.ensure_profile(USER_A, "a@example.com")
    tenant.ensure_profile(USER_B, "b@example.com")
    tenant.claim_slot(1, USER_A, None)
    tenant.slots[1]["imei2"] = IMEI2
    svc, jobs, assign = _farm_svc(tmp_path, tenant)
    result = svc.assign_slot(1, _assign_body(USER_B))
    assert result.http_status == 409
    assert list(jobs._conn.execute("SELECT job_id FROM vps_jobs")) == []
    assert assign.is_assigned(1) is False
    assert tenant.owner_of_slot(1) == USER_A


def test_missing_lovable_credential_fails_closed(tmp_path: Path):
    tenant = UnconfiguredTenantStore()
    svc, jobs, assign = _farm_svc(tmp_path, tenant)
    result = svc.assign_slot(1, _assign_body(USER_A))
    assert result.http_status == 503
    assert result.body["error"] == "auth_not_configured"
    assert list(jobs._conn.execute("SELECT job_id FROM vps_jobs")) == []
    assert assign.is_assigned(1) is False


def test_gateway_rest_does_not_fall_back_to_anon():
    http = FakeHTTP({})
    gw = SupabaseGateway("https://example.supabase.co", "anon-test-key", session=http)
    assert gw.has_service_role is False
    assert gw.rest_select("slots", query="select=id") is None
    assert gw.rest_insert("messages", {"message_id": "x"}) is None
    assert http.calls == []


def test_signup_does_not_use_vps_sqlite_passwords():
    gotrue = MemoryGoTrue()
    svc = AuthService(supabase=gotrue, tenant=UnconfiguredTenantStore())
    created = svc.signup({"email": "pw@example.com", "password": STRONG})
    assert created.http_status == 200
    raw = json.dumps(created.body)
    assert STRONG not in raw
    assert "password_hash" not in raw


def test_http_user_isolation_and_authoritative_imei2(tmp_path: Path):
    server, port, tenant, _auth = _start_auth_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        _, a, _ = _http("POST", f"{base}/auth/signup", body={"email": "la@example.com", "password": STRONG})
        _, b, _ = _http("POST", f"{base}/auth/signup", body={"email": "lb@example.com", "password": STRONG})
        token_a = a["session"]["access_token"]
        token_b = b["session"]["access_token"]
        user_a = a["user"]["id"]
        status, farm_job, _ = _http(
            "POST",
            f"{base}/farm/slots/1/assign",
            token=FARM_TOKEN,
            body={
                "rental_id": str(uuid.uuid4()),
                "esim_qr_url": "https://example.test/private/qr",
                "carrier": "Verizon",
                "user_id": user_a,
                "imei2": "000000000000000",
            },
        )
        assert status == 202
        tenant.slots[1]["imei2"] = IMEI2
        tenant.slots[1]["carrier_name"] = "Verizon"
        status, listed, _ = _http("GET", f"{base}/slots", token=token_a)
        assert status == 200
        assert listed["slots"][0]["imei2"] == IMEI2
        assert listed["slots"][0]["carrier_name"] == "Verizon"
        status, hidden, _ = _http("GET", f"{base}/slots/{SLOT1}", token=token_b)
        assert status == 404
        status, stolen, _ = _http(
            "POST",
            f"{base}/slots/{SLOT1}/esim",
            token=token_b,
            body={"rental_id": str(uuid.uuid4()), "qr_code_url": "users/b/esim", "carrier": "Verizon"},
        )
        assert status == 404
        status, farm_sess, _ = _http("GET", f"{base}/auth/session", token=FARM_TOKEN)
        assert farm_sess["token_type"] == "FARM_SERVICE_TOKEN"
        assert MACHINE_TOKEN not in json.dumps(listed)
    finally:
        server.shutdown()


def test_openapi_has_no_machine_token_and_describes_supabase_auth():
    raw = json.dumps(build_vps_openapi_document())
    assert "VPS_TO_LOVABLE_API_TOKEN=" not in raw
    assert MACHINE_TOKEN not in raw
    assert "does not store" in raw.lower() or "never stored" in raw.lower()
    assert "FARM_SERVICE_TOKEN" in raw
    assert "FARM_AGENT_API_TOKEN" in raw
    assert "VOIDFIX_WEBHOOK_SECRET" in raw
    assert "imei2" in raw
    assert "carrier_name" in raw


def test_production_validate_requires_lovable_machine_token(monkeypatch, tmp_path: Path):
    from infrastructure.production_validate import validate_production_config

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("HARDWARE_AGENT_TOKEN", "token")
    monkeypatch.setenv("MOBI_RENT_DEPLOY_ROLE", "vps")
    monkeypatch.setenv("VOIDFIX_DEVICE_MAP_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("FARM_AGENT_API_TOKEN", "farm-agent-token")
    monkeypatch.delenv("LOVABLE_API_URL", raising=False)
    monkeypatch.delenv("VPS_TO_LOVABLE_API_TOKEN", raising=False)
    report = validate_production_config(env_file=None, project_root=tmp_path, role="vps")
    codes = {i.code for i in report.issues}
    assert "lovable_api_url" in codes
    assert "lovable_machine_token" in codes
