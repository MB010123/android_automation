"""Customer Reconnect Cellular uses existing airplane_cycle (currently 501)."""
from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_agent_tasks import FarmTaskRequest, execute_farm_task
from application.farm_task_executor import AIRPLANE_UNSUPPORTED_REASON, FarmTaskExecutorDeps
from application.vps_lovable_routes import parse_route
from tests.fakes_supabase import MemoryTenant
from tests.test_farm_agent_tasks import FakeRunner
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeFarm,
    FakePlatform,
    SLOT1_SERIAL,
    SLOT2_SERIAL,
    _rental,
    _service,
)


def test_reconnect_cellular_route_is_semantic():
    rental = "00000000-0000-4000-8000-000000000099"
    parsed = parse_route(f"/rentals/{rental}/remote-access/reconnect-cellular")
    assert parsed is not None
    assert parsed.kind == "remote_access_action"
    assert parsed.action == "reconnect-cellular"


def test_owned_slot_only_and_cross_rental_rejected(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=farm,
        allowed=(1, 2),
        workspace_map={1: "ws-1", 2: "ws-2"},
    )
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=2, user_id=CUSTOMER_B)
    denied = service.reconnect_cellular_for_customer(CUSTOMER_B, rental_a, {})
    assert denied.http_status == 403
    assert denied.body["error"] == "rental_not_owned"
    missing = service.reconnect_cellular_for_customer(CUSTOMER_A, "00000000-0000-4000-8000-000000000001", {})
    assert missing.http_status == 404
    owned = service.reconnect_cellular_for_customer(CUSTOMER_A, rental_a, {})
    assert owned.http_status == 501
    assert owned.body["error"] == "action_not_supported"
    assert SLOT1_SERIAL not in json.dumps(owned.body)
    cycles = [t for t in farm.tasks if t["type"] == "airplane_cycle"]
    assert len(cycles) == 1
    assert cycles[0]["slot"] == 1
    assert cycles[0]["payload"] == {}
    other = service.reconnect_cellular_for_customer(CUSTOMER_B, rental_b, {})
    assert other.http_status == 501
    slots = [t["slot"] for t in farm.tasks if t["type"] == "airplane_cycle"]
    assert slots == [1, 2]


def test_identity_payload_rejected_without_farm_call(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    spoof = service.reconnect_cellular_for_customer(
        CUSTOMER_A, rental, {"action": "reconnect", "serial": SLOT2_SERIAL, "slot_id": 2}
    )
    assert spoof.http_status == 403
    assert spoof.body["error"] == "forbidden_control"
    assert not any(t["type"] == "airplane_cycle" for t in farm.tasks)


def test_concurrent_reconnect_is_rejected(tmp_path: Path):
    tenant = MemoryTenant()
    started = threading.Event()
    release = threading.Event()

    class HangFarm(FakeFarm):
        def run_task(self, **kwargs):
            started.set()
            assert release.wait(2)
            return super().run_task(**kwargs)

    farm = HangFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    first_status = []

    def _first():
        first_status.append(service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {}))

    thread = threading.Thread(target=_first)
    thread.start()
    assert started.wait(2)
    second = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert second.http_status == 409
    assert second.body["error"] == "remote_access_busy"
    release.set()
    thread.join(timeout=2)
    assert first_status and first_status[0].http_status == 501
    assert [t["slot"] for t in farm.tasks if t["type"] == "airplane_cycle"] == [1]


def test_cooldown_and_timeout(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, clock = _service(
        tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    first = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert first.http_status == 501
    second = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert second.http_status == 429
    assert second.body["error"] == "rate_limited"
    assert second.body["retry_after"] >= 1
    assert len([t for t in farm.tasks if t["type"] == "airplane_cycle"]) == 1
    clock.now += 31
    third = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert third.http_status == 501
    farm.airplane_timeout = True
    clock.now += 31
    timed = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert timed.http_status == 504
    assert timed.body["error"] == "timeout"
    _ = store


def test_airplane_cycle_does_not_mutate_esim_voidfix_or_wipe():
    runner = FakeRunner()
    result = execute_farm_task(
        adb_path="adb",
        slot_map={1: "SERIAL-A", 2: "SERIAL-B"},
        request=FarmTaskRequest(
            job_id="job-air",
            task_type="airplane_cycle",
            farm_slot_id=2,
            payload={},
        ),
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.http_status == 501
    assert result.error == "action_not_supported"
    assert AIRPLANE_UNSUPPORTED_REASON in (result.message or "")
    assert runner.calls == []
    source = (ROOT / "application" / "farm_task_executor.py").read_text(encoding="utf-8")
    assert "run_airplane_cycle_if_supported" in source
    assert "raw adb shell settings are not exposed" in source


def test_stream_setup_mode_and_phone_ready_unchanged(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    inspects_before = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    denied = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert denied.http_status == 501
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT1_SERIAL
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert home.http_status == 403
    inspects_after = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert len(inspects_after) >= len(inspects_before)
    session = store.get(rental)
    store.upsert(
        replace(
            session,
            setup_phase="complete",
            setup_complete=True,
            activation_observed="confirmed",
        )
    )
    service._reconnect_last.clear()
    again = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert again.http_status == 501
    home_ready = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert home_ready.http_status == 200
    assert not any(t["type"] == "setup_session_voidfix_cycle" for t in farm.tasks)
    assert not any(t["type"] == "voidfix_repair" for t in farm.tasks)
    cycles = [t for t in farm.tasks if t["type"] == "airplane_cycle"]
    assert all(t["slot"] == 1 for t in cycles)
    assert all("euicc" not in json.dumps(t["payload"]).lower() for t in cycles)
