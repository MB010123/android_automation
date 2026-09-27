"""Fixes for pre-deploy blockers: tenant messages, farm ownership, service role."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.auth_service import AuthService
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from application.vps_slot_sms_service import VpsSlotSmsService
from infrastructure.farm_sms_client import FarmDispatchResponse
from infrastructure.outbound_message_store import OutboundMessageRecord, OutboundMessageStore
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.supabase_gateway import SupabaseGateway
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryGoTrue, MemoryTenant, seed_owned_slot
from tests.test_supabase_auth import FakeHTTP
from tests.test_vps_user_auth import (
    FARM_TOKEN,
    STRONG,
    _http,
    _start_auth_server,
)

SLOT1 = public_id_for_farm_slot(1)


class _FarmOk:
    def send_sms(self, **kwargs):
        return FarmDispatchResponse(ok=True, http_status=200, body={"ok": True}, error=None)


class _SyncWorker(VpsJobWorker):
    def enqueue_process(self, job_id: str) -> None:
        return None


def _farm_svc(tmp_path: Path, tenant: MemoryTenant | None = None):
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    worker = _SyncWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=None,
        poll_interval_seconds=3600.0,
        auth_store=tenant,
    )
    svc = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=lambda: {"ok": True, "offline_slots": [], "slot_count": 20, "adb_online": 20},
        known_farm_slots={1, 2},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
        esim_url_prefixes=("https://example.test/",),
    )
    return svc, jobs, assign


def _assign_body(user_id: str | None = None) -> dict:
    body = {
        "rental_id": str(uuid.uuid4()),
        "esim_qr_url": "https://example.test/private/qr",
        "carrier": "T-Mobile",
    }
    if user_id:
        body["user_id"] = user_id
    return body


def test_unowned_slot_assignment_rejected(tmp_path: Path):
    tenant = MemoryTenant()
    user = str(uuid.uuid4())
    tenant.ensure_profile(user, "a@example.com")
    svc, jobs, assign = _farm_svc(tmp_path, tenant)
    result = svc.assign_slot(1, _assign_body(user))
    assert result.http_status == 404
    assert list(jobs._conn.execute("SELECT job_id FROM vps_jobs")) == []
    assert assign.is_assigned(1) is False


def test_same_user_retry_does_not_steal(tmp_path: Path):
    tenant = MemoryTenant()
    user = str(uuid.uuid4())
    tenant.ensure_profile(user, "a@example.com")
    svc, _jobs, _assign = _farm_svc(tmp_path, tenant)
    payload = _assign_body(user)
    seed_owned_slot(
        tenant,
        bay=1,
        rental_id=str(payload["rental_id"]),
        user_id=user,
        qr_code_url=str(payload["esim_qr_url"]),
        carrier_name="T-Mobile",
    )
    first = svc.assign_slot(1, payload)
    assert first.http_status == 202
    replay = svc.assign_slot(1, payload)
    assert replay.http_status == 202
    assert replay.body["job_id"] == first.body["job_id"]
    assert tenant.owner_of_slot(1) == user


def test_other_owner_rejected_before_job(tmp_path: Path):
    tenant = MemoryTenant()
    owner = str(uuid.uuid4())
    other = str(uuid.uuid4())
    tenant.ensure_profile(owner, "a@example.com")
    tenant.ensure_profile(other, "b@example.com")
    svc, jobs, assign = _farm_svc(tmp_path, tenant)
    rental = str(uuid.uuid4())
    seed_owned_slot(tenant, bay=1, rental_id=rental, user_id=owner, qr_code_url="https://example.test/private/qr")
    before = list(jobs._conn.execute("SELECT job_id FROM vps_jobs"))
    body = _assign_body(other)
    body["rental_id"] = rental
    result = svc.assign_slot(1, body)
    assert result.http_status == 409
    assert result.body["error"] == "slot_unavailable"
    after = list(jobs._conn.execute("SELECT job_id FROM vps_jobs"))
    assert after == before
    assert assign.is_assigned(1) is False
    assert tenant.owner_of_slot(1) == owner


def test_missing_service_role_rejects_assign_without_job(tmp_path: Path):
    tenant = MemoryTenant()
    tenant.privileged = False
    user = str(uuid.uuid4())
    tenant.ensure_profile(user, "a@example.com")
    svc, jobs, assign = _farm_svc(tmp_path, tenant)
    result = svc.assign_slot(1, _assign_body(user))
    assert result.http_status == 503
    assert result.body["error"] == "auth_not_configured"
    assert list(jobs._conn.execute("SELECT job_id FROM vps_jobs")) == []
    assert assign.is_assigned(1) is False


def test_gateway_rest_requires_service_role():
    http = FakeHTTP({})
    gw = SupabaseGateway(
        "https://example.supabase.co",
        "anon-test-key",
        session=http,  # type: ignore[arg-type]
    )
    assert gw.has_service_role is False
    assert gw.rest_select("slots", query="select=id") is None
    assert gw.rest_insert("messages", {"message_id": "x"}) is None
    assert http.calls == []


def test_user_message_from_supabase_not_sqlite(tmp_path: Path):
    tenant = MemoryTenant()
    user = str(uuid.uuid4())
    tenant.ensure_profile(user, "a@example.com")
    tenant.claim_slot(1, user, None)
    slot_uuid = tenant.slots[1]["id"]
    tenant.messages["msg-owned"] = {
        "message_id": "msg-owned",
        "slot_id": slot_uuid,
        "direction": "outbound",
        "phone_number": "+15555550100",
        "message_body": "hello",
        "status": "queued",
    }
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
    own = sms.get_tenant_message("msg-owned", user_id=user)
    assert own.http_status == 200
    assert own.body["message_id"] == "msg-owned"
    assert own.body["body"] == "hello"
    other = sms.get_tenant_message("msg-owned", user_id=str(uuid.uuid4()))
    assert other.http_status == 404
    sqlite_only = sms.get_tenant_message("sqlite-only", user_id=user)
    assert sqlite_only.http_status == 404
    farm = sms.get_message("sqlite-only")
    assert farm.http_status == 200
    assert farm.body["message_id"] == "sqlite-only"


def test_http_user_cannot_read_other_message(tmp_path: Path):
    server, port, tenant, _auth = _start_auth_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        _, a, _ = _http("POST", f"{base}/auth/signup", body={"email": "a@example.com", "password": STRONG})
        _, b, _ = _http("POST", f"{base}/auth/signup", body={"email": "b@example.com", "password": STRONG})
        token_a = a["session"]["access_token"]
        token_b = b["session"]["access_token"]
        user_a = a["user"]["id"]
        tenant.claim_slot(1, user_a, None)
        mid = str(uuid.uuid4())
        tenant.messages[mid] = {
            "message_id": mid,
            "slot_id": tenant.slots[1]["id"],
            "direction": "outbound",
            "phone_number": "+15555550100",
            "message_body": "secret",
            "status": "sent",
        }
        status, mine, _ = _http("GET", f"{base}/messages/{mid}", token=token_a)
        assert status == 200
        assert mine["message_id"] == mid
        status, hidden, _ = _http("GET", f"{base}/messages/{mid}", token=token_b)
        assert status == 404
        status, farm, _ = _http("GET", f"{base}/messages/{mid}", token=FARM_TOKEN)
        assert status == 404
    finally:
        server.shutdown()


def test_auth_service_still_uses_gotrue_without_service_role():
    gotrue = MemoryGoTrue()
    svc = AuthService(supabase=gotrue, tenant=MemoryTenant())
    created = svc.signup({"email": "c@example.com", "password": STRONG})
    assert created.http_status == 200
    assert created.access_token
