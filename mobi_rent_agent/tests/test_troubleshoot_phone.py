"""Customer Troubleshoot Phone: diagnostics + allowlisted Farm reboot only."""
from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_lovable_routes import parse_route
from tests.fakes_supabase import MemoryTenant
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


def test_troubleshoot_route_is_semantic():
    rental = "00000000-0000-4000-8000-000000000099"
    parsed = parse_route(f"/rentals/{rental}/remote-access/troubleshoot")
    assert parsed is not None
    assert parsed.action == "troubleshoot"


def test_diagnostics_ownership_and_cross_rental(tmp_path: Path):
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
    stolen = service.troubleshoot_for_customer(CUSTOMER_B, rental_a, {})
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    missing = service.troubleshoot_for_customer(
        CUSTOMER_A, "00000000-0000-4000-8000-000000000001", {}
    )
    assert missing.http_status == 404
    ok = service.troubleshoot_for_customer(CUSTOMER_A, rental_a, {})
    assert ok.http_status == 200
    assert ok.body["ok"] is True
    assert ok.body["slot_id"] == 1
    assert ok.body["phone"]["state"] == "online"
    assert ok.body["cellular_status"] == "unknown"
    assert "reboot" in ok.body["supported_recovery"]
    assert SLOT1_SERIAL not in json.dumps(ok.body)
    assert "workspace" not in json.dumps(ok.body)
    other = service.troubleshoot_for_customer(CUSTOMER_B, rental_b, {})
    assert other.http_status == 200
    assert other.body["slot_id"] == 2


def test_customer_cannot_name_another_device(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    spoof = service.troubleshoot_for_customer(
        CUSTOMER_A, rental, {"serial": SLOT2_SERIAL, "slot_id": 2}
    )
    assert spoof.http_status == 403
    assert spoof.body["error"] == "forbidden_control"
    shell = service.troubleshoot_for_customer(CUSTOMER_A, rental, {"action": "adb"})
    assert shell.http_status == 403
    air = service.troubleshoot_for_customer(CUSTOMER_A, rental, {"action": "airplane_cycle"})
    assert air.http_status == 403
    assert not any(t["type"] == "reboot" for t in farm.tasks)


def test_concurrent_and_cooldown(tmp_path: Path):
    tenant = MemoryTenant()
    started = threading.Event()
    release = threading.Event()

    class HangFarm(FakeFarm):
        def run_task(self, **kwargs):
            if kwargs.get("task_type") == "reboot":
                started.set()
                assert release.wait(2)
            return super().run_task(**kwargs)

    farm = HangFarm()
    service, _, clock = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    first_status = []

    def _first():
        first_status.append(
            service.troubleshoot_for_customer(CUSTOMER_A, rental, {"action": "reboot"})
        )

    thread = threading.Thread(target=_first)
    thread.start()
    assert started.wait(2)
    second = service.troubleshoot_for_customer(CUSTOMER_A, rental, {})
    assert second.http_status == 409
    release.set()
    thread.join(timeout=2)
    assert first_status and first_status[0].http_status == 202
    cooled = service.troubleshoot_for_customer(CUSTOMER_A, rental, {})
    assert cooled.http_status == 429
    clock.now += 11
    again = service.troubleshoot_for_customer(CUSTOMER_A, rental, {})
    assert again.http_status == 200


def test_farm_timeout_and_unavailable(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.reboot_timeout = True
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    timed = service.troubleshoot_for_customer(CUSTOMER_A, rental, {"action": "reboot"})
    assert timed.http_status == 504
    assert timed.body["error"] == "timeout"
    service2, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=None)
    rental2 = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    missing = service2.troubleshoot_for_customer(CUSTOMER_A, rental2, {"action": "reboot"})
    assert missing.http_status == 503
    assert missing.body["error"] == "farm_unreachable"


def test_reboot_uses_existing_farm_reboot_only(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    before = store.get(rental)
    result = service.troubleshoot_for_customer(CUSTOMER_A, rental, {"action": "reboot"})
    assert result.http_status == 202
    assert result.body["recovery"] == "reboot_requested"
    assert result.body["ok"] is True
    reboots = [t for t in farm.tasks if t["type"] == "reboot"]
    assert len(reboots) == 1
    assert reboots[0]["slot"] == 1
    assert reboots[0]["payload"] == {}
    assert not any(t["type"] == "airplane_cycle" for t in farm.tasks)
    assert not any(t["type"] == "assign" for t in farm.tasks)
    assert not any(t["type"] == "voidfix_repair" for t in farm.tasks)
    assert not any(t["type"] == "setup_session_voidfix_cycle" for t in farm.tasks)
    after = store.get(rental)
    assert after.activation_observed == before.activation_observed
    assert after.setup_complete == before.setup_complete
    assert after.prepare_state == before.prepare_state
    blob = json.dumps(result.body)
    assert "factory_reset" in result.body["unsupported"]
    assert SLOT1_SERIAL not in blob


def test_diagnostics_do_not_change_setup_mode_stream_or_phone_ready(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    diag = service.troubleshoot_for_customer(CUSTOMER_A, rental, {})
    assert diag.http_status == 200
    assert diag.body["setup_complete"] is False
    assert diag.body["ui_state"] != "phone_ready"
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT1_SERIAL
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert home.http_status == 403
    session = store.get(rental)
    store.upsert(
        replace(
            session,
            setup_phase="complete",
            setup_complete=True,
            activation_observed="confirmed",
        )
    )
    service._troubleshoot_last.clear()
    ready = service.troubleshoot_for_customer(CUSTOMER_A, rental, {})
    assert ready.http_status == 200
    assert ready.body["ui_state"] == "phone_ready"
    assert ready.body["activation_state"] == "ACTIVE"
    home_ready = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert home_ready.http_status == 200
    inspects = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects
    assert not any(t["type"] == "reboot" for t in farm.tasks)
