"""Rapid poll resilience: flaky auth/tenant must not flap ownership or 503."""
from __future__ import annotations

import hashlib
import random
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.auth_service import AuthService
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from infrastructure.auth_models import AuthUser
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.supabase_gateway import SupabaseGateway
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryTenant, seed_owned_slot
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    FakeFarm,
    FakePlatform,
    _rental,
    _service,
)
from tests.test_supabase_auth import USER, FakeHTTP, FakeResponse

SLOT4_SERIAL = "SERIAL-SLOT4-TEST"
BAY = 4
POLL_ERROR_CODES = frozenset({"auth_unavailable", "rental_not_owned", "rental_expired"})


class FlakyTenant(MemoryTenant):
    def __init__(self, fail_rate: float = 0.3, seed: int = 4) -> None:
        super().__init__()
        self._fail_rate = fail_rate
        self._rng = random.Random(seed)
        self.lookups = 0
        self.failures = 0
        self._lock = threading.Lock()

    def _roll(self) -> bool:
        with self._lock:
            self.lookups += 1
            return self._rng.random() < self._fail_rate

    def _maybe_fail(self) -> None:
        if self._roll():
            with self._lock:
                self.failures += 1
            raise OSError("lovable_unavailable")

    def get_slot_by_id(self, slot_id: str):
        self._maybe_fail()
        row = super().get_slot_by_id(slot_id)
        if row is not None and self._roll():
            expired = dict(row)
            expired["user_id"] = None
            expired["status"] = "expired"
            return expired
        return row

    def get_slot_row(self, farm_slot_id: int):
        self._maybe_fail()
        row = super().get_slot_row(farm_slot_id)
        if row is not None and self._roll():
            expired = dict(row)
            expired["status"] = "expired"
            return expired
        return row

    def owner_of_slot(self, farm_slot_id: int):
        self._maybe_fail()
        if self._roll():
            return None
        return super().owner_of_slot(farm_slot_id)


class FlakyGoTrueHTTP(FakeHTTP):
    def __init__(self, fail_rate: float = 0.3, seed: int = 7) -> None:
        super().__init__(
            {("GET", "https://example.supabase.co/auth/v1/user"): FakeResponse(200, USER)}
        )
        self._fail_rate = fail_rate
        self._rng = random.Random(seed)
        self.calls_n = 0

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls_n += 1
        if self._rng.random() < self._fail_rate:
            raise ConnectionError("gotrue_flaky")
        return super().request(method, url, json=json, headers=headers, timeout=timeout)


def _farm_service(tmp_path: Path, tenant: MemoryTenant) -> VpsFarmManagementService:
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    worker = VpsJobWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=None,
        poll_interval_seconds=3600.0,
        auth_store=tenant,
    )
    return VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": list(range(1, 22)),
            "slot_count": 21,
            "adb_online": 21,
        },
        known_farm_slots=set(range(1, 22)),
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
    )


def test_validate_user_token_retries_and_serves_cache_on_gotrue_outage():
    http = FlakyGoTrueHTTP(fail_rate=1.0)
    gw = SupabaseGateway(
        "https://example.supabase.co",
        "anon-test-key",
        session=http,  # type: ignore[arg-type]
    )
    svc = AuthService(supabase=gw, jwt_secret="")
    svc._remember_token(
        hashlib.sha256(b"supabase-access-token").hexdigest(),
        AuthUser(user_id=USER["id"], email=USER["email"], is_active=True, email_verified=True),
        None,
        svc._clock(),
    )
    ctx = svc.validate_user_token("supabase-access-token")
    assert ctx is not None
    assert ctx.user is not None
    assert ctx.user.user_id == USER["id"]


def test_validate_user_token_retries_once_on_new_token():
    class OnceThenOk(FakeHTTP):
        def __init__(self) -> None:
            super().__init__(
                {("GET", "https://example.supabase.co/auth/v1/user"): FakeResponse(200, USER)}
            )
            self.n = 0

        def request(self, method, url, json=None, headers=None, timeout=None):
            self.n += 1
            if self.n == 1:
                raise ConnectionError("first")
            return super().request(method, url, json=json, headers=headers, timeout=timeout)

    http = OnceThenOk()
    gw = SupabaseGateway(
        "https://example.supabase.co",
        "anon-test-key",
        session=http,  # type: ignore[arg-type]
    )
    svc = AuthService(supabase=gw, jwt_secret="")
    ctx = svc.validate_user_token("supabase-access-token")
    assert ctx is not None and ctx.user is not None
    assert ctx.user.user_id == USER["id"]
    assert http.n == 2
    again = svc.validate_user_token("supabase-access-token")
    assert again is not None
    assert http.n == 2


def test_tenant_slot_row_keeps_last_good_when_refresh_returns_expired(tmp_path: Path):
    tenant = MemoryTenant()
    seed_owned_slot(tenant, bay=BAY, rental_id="active-rental-4", user_id=CUSTOMER_A)
    tenant.slots[BAY]["status"] = "active"
    farm_svc = _farm_service(tmp_path, tenant)
    good = farm_svc._tenant_slot_row(BAY)
    assert good.get("user_id") == CUSTOMER_A
    tenant.slots[BAY] = {
        "id": "historical-expired-4",
        "user_id": "someone-else",
        "rental_id": "historical-expired-4",
        "motherboard_slot_num": BAY,
        "status": "expired",
    }
    kept = farm_svc._tenant_slot_row(BAY)
    assert kept.get("user_id") == CUSTOMER_A
    assert kept.get("id") == "active-rental-4"
    assert str(kept.get("status") or "").lower() != "expired"


def test_fifty_concurrent_rental_polls_survive_flaky_tenant_and_snapshot(tmp_path: Path):
    tenant = FlakyTenant(fail_rate=0.0)
    platform = FakePlatform(registered={SLOT4_SERIAL})
    service, _store, _clock = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=FakeFarm(),
        allowed=(4,),
        slot_map={4: SLOT4_SERIAL},
        workspace_map={4: "ws-slot4"},
        gads_slot_ids=(4,),
        prepare_slot_ids=(4,),
        observe_slot_ids=(4,),
    )
    rental = _rental(tenant, bay=BAY, user_id=CUSTOMER_A, status="active")
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201, created.body
    _clock.sleep = lambda _seconds: None  # type: ignore[method-assign]

    farm_svc = _farm_service(tmp_path, tenant)
    farm_svc.start_inventory_snapshot(interval_seconds=0.05)
    assert farm_svc._tenant_slot_row(BAY).get("user_id") == CUSTOMER_A
    tenant._fail_rate = 0.3

    errors: list[str] = []
    lock = threading.Lock()

    def poll() -> None:
        for _ in range(8):
            for result in (
                service.screen_guidance_for_customer(CUSTOMER_A, rental),
                service.device_status_for_customer(CUSTOMER_A, rental),
                service.get_remote_access(CUSTOMER_A, None, rental),
            ):
                code = str((result.body or {}).get("error") or "")
                status = str((result.body or {}).get("status") or "")
                with lock:
                    if code in POLL_ERROR_CODES:
                        errors.append(code)
                    if status == "expired":
                        errors.append("expired")
                    if result.http_status == 503:
                        errors.append(str(code or "503"))
            time.sleep(0.01)

    threads = [threading.Thread(target=poll) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    farm_svc.stop_inventory_snapshot()
    assert errors == []
    listed = farm_svc.get_slot_status(public_id_for_farm_slot(BAY))
    assert listed.http_status == 200
    assert listed.body.get("user_id") == CUSTOMER_A
    assert listed.body.get("hold_reason") != "expired"
    assert tenant.failures > 0
