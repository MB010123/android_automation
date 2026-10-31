"""4-stream FIFO queue, idle/TTL promotion, admin bypass, fast nav, HTTP pool."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.admin_live_access_service import AdminLiveAccessService
from application.customer_stream_queue import CustomerStreamQueue
from application.vps_lovable_routes import parse_route
from infrastructure.admin_live_access_store import AdminLiveAccessStore
from infrastructure.farm_task_client import FarmTaskClient
from infrastructure.http_pool import NodelayHTTPAdapter, pooled_session
from tests.fakes_supabase import MemoryTenant
from tests.test_admin_live_access import _admin
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    SLOT1_SERIAL,
    FakeClock,
    FakeFarm,
    FakePlatform,
    _rental,
    _service,
)

SERIALS = {n: f"SERIAL-Q{n}" for n in range(1, 6)}


def _queue_service(tmp_path: Path, *, clock: FakeClock | None = None):
    clock = clock or FakeClock()
    tenant = MemoryTenant()
    platform = FakePlatform(registered=set(SERIALS.values()))
    service, store, clock = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=FakeFarm(),
        allowed=tuple(range(1, 6)),
        slot_map=SERIALS,
        workspace_map={n: f"ws-q{n}" for n in range(1, 6)},
        gads_slot_ids=tuple(range(1, 6)),
        prepare_slot_ids=tuple(range(1, 6)),
        observe_slot_ids=tuple(range(1, 6)),
        clock=clock,
    )
    service._stream_queue = CustomerStreamQueue(
        clock=clock,
        max_concurrent=4,
        ttl_seconds=480,
        on_busy_change=service._stream_queue.on_busy_change,
    )
    return service, store, clock, tenant, platform


def _rentals(tenant: MemoryTenant) -> list[str]:
    return [_rental(tenant, bay=n, user_id=CUSTOMER_A, status="active") for n in range(1, 6)]


def test_fifth_customer_is_queued_for_eight_minutes(tmp_path: Path):
    service, _, _, tenant, _ = _queue_service(tmp_path)
    rentals = _rentals(tenant)
    created = [service.create_remote_access(CUSTOMER_A, None, rid) for rid in rentals[:4]]
    assert all(item.http_status == 201 for item in created)
    assert all(item.body.get("queue_status") == "active" for item in created)
    fifth = service.create_remote_access(CUSTOMER_A, None, rentals[4])
    assert fifth.http_status == 200
    assert fifth.body["queue_status"] == "queued"
    assert fifth.body["status"] == "queued"
    assert fifth.body["queue_position"] == 1
    assert fifth.body["estimated_wait_minutes"] == 8
    assert fifth.body["message"] == "Up next in 8 minutes"
    assert fifth.body["stream_url"] is None
    polled = service.get_remote_access(CUSTOMER_A, None, rentals[4])
    assert polled.body["queue_status"] == "queued"
    status = service.device_status_for_customer(CUSTOMER_A, rentals[4])
    assert status.body["queue_status"] == "queued"
    assert not any(t["type"] == "device_screen_guidance" for t in service._farm.tasks if t.get("slot") == 5)


def test_release_stream_promotes_queued_rental(tmp_path: Path):
    service, _, _, tenant, _ = _queue_service(tmp_path)
    rentals = _rentals(tenant)
    for rid in rentals[:4]:
        assert service.create_remote_access(CUSTOMER_A, None, rid).http_status == 201
    assert service.create_remote_access(CUSTOMER_A, None, rentals[4]).body["queue_status"] == "queued"
    released = service.release_stream(CUSTOMER_A, rentals[0])
    assert released.http_status == 200
    assert released.body["released"] is True
    promoted = service.get_remote_access(CUSTOMER_A, None, rentals[4])
    assert promoted.http_status in {200, 201}
    assert promoted.body.get("queue_status") == "active"
    assert promoted.body.get("status") != "queued"


def test_ttl_and_idle_promote_queued_rental(tmp_path: Path):
    clock = FakeClock()
    service, _, clock, tenant, _ = _queue_service(tmp_path, clock=clock)
    rentals = _rentals(tenant)
    for rid in rentals[:4]:
        assert service.create_remote_access(CUSTOMER_A, None, rid).http_status == 201
    assert service.create_remote_access(CUSTOMER_A, None, rentals[4]).body["queue_status"] == "queued"
    elapsed = 0
    while elapsed < 470:
        step = min(40, 470 - elapsed)
        clock.now += step
        elapsed += step
        for rid in rentals[:4]:
            assert service.get_remote_access(CUSTOMER_A, None, rid).body.get("queue_status") == "active"
        assert service.get_remote_access(CUSTOMER_A, None, rentals[4]).body.get("queue_status") == "queued"
    clock.now += 11
    promoted = service.get_remote_access(CUSTOMER_A, None, rentals[4])
    assert promoted.body.get("queue_status") == "active"

    clock = FakeClock()
    service, _, clock, tenant, _ = _queue_service(tmp_path, clock=clock)
    rentals = _rentals(tenant)
    for rid in rentals[:4]:
        assert service.create_remote_access(CUSTOMER_A, None, rid).http_status == 201
    assert service.create_remote_access(CUSTOMER_A, None, rentals[4]).body["queue_status"] == "queued"
    clock.now += 46
    idle_promoted = service.device_status_for_customer(CUSTOMER_A, rentals[4])
    assert idle_promoted.body.get("queue_status") == "active"


def test_admin_live_view_bypasses_four_stream_limit(tmp_path: Path):
    service, _, clock, tenant, platform = _queue_service(tmp_path)
    rentals = _rentals(tenant)
    for rid in rentals[:4]:
        assert service.create_remote_access(CUSTOMER_A, None, rid).http_status == 201
    fifth = service.create_remote_access(CUSTOMER_A, None, rentals[4])
    assert fifth.body["queue_status"] == "queued"
    admin = AdminLiveAccessService(
        enabled=True,
        slot_device_map=SERIALS,
        platform=platform,
        store=AdminLiveAccessStore(tmp_path / "admin-q.sqlite"),
        farm_task_client=FakeFarm(),
        farm_status_fetcher=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": list(range(1, 6)),
        },
        workspace_map={n: f"ws-q{n}" for n in range(1, 6)},
        clock=clock,
        stream_queue=service._stream_queue,
    )
    opened = admin.create_session(_admin(), 1)
    assert opened.http_status in {200, 201}
    assert opened.body.get("queue_status") != "queued"
    assert service._stream_queue.active_count() == 4
    assert service.get_remote_access(CUSTOMER_A, None, rentals[4]).body["queue_status"] == "queued"


def test_pooled_http_session_uses_keepalive_adapter():
    session = pooled_session()
    adapter = session.get_adapter("http://farm.example/")
    assert isinstance(adapter, NodelayHTTPAdapter)
    client = FarmTaskClient("http://farm.example", "tok")
    assert isinstance(client._session.get_adapter("http://farm.example/"), NodelayHTTPAdapter)
    assert session.headers.get("Connection") == "keep-alive"


def test_fast_nav_control_returns_under_150ms(tmp_path: Path):
    class SlowInspectFarm(FakeFarm):
        def run_task(self, **kwargs):
            if kwargs.get("task_type") == "setup_session_inspect":
                time.sleep(0.4)
            return super().run_task(**kwargs)

    tenant = MemoryTenant()
    farm = SlowInspectFarm()
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(registered={SLOT1_SERIAL}),
        farm=farm,
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, status="active")
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    started = time.perf_counter()
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert home.http_status == 200, home.body
    assert elapsed_ms < 150
    assert not any(t["type"] == "setup_session_inspect" for t in farm.tasks)


def test_release_stream_route_is_parsed():
    rental = "00000000-0000-4000-8000-000000000004"
    route = parse_route(f"/rentals/{rental}/release-stream")
    assert route is not None
    assert route.kind == "rental_release_stream"
    assert route.rental_id == rental


def test_queue_wait_estimate_uses_soonest_expiry():
    clock = FakeClock()
    queue = CustomerStreamQueue(clock=clock, max_concurrent=4, ttl_seconds=480)
    for index in range(4):
        decision = queue.admit(f"r{index}", CUSTOMER_A, index + 1, claim=True)
        assert decision.queue_status == "active"
    queued = queue.admit("r4", CUSTOMER_A, 5, claim=True)
    assert queued.queue_status == "queued"
    assert queued.queue_position == 1
    assert queued.estimated_wait_minutes == 8
    assert queued.message == "Up next in 8 minutes"
