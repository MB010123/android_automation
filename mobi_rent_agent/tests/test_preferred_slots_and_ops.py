"""Preferred next slot, rental disable, about-phone, and airplane farm tasks."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.device_airplane_mode import parse_airplane_state, run_airplane_mode
from application.device_data_cleanup_farm_task import LOCKDOWN_PACKAGES
from application.farm_task_types import FarmTaskRequest, SUPPORTED_TASK_TYPES
from application.preferred_slots import parse_preferred_next_slot_ids, sort_available_bays
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from application.vps_lovable_routes import parse_route
from infrastructure.adb_companion import AdbCommandResult, AdbCommandRunner
from infrastructure.device_cleanup_store import DeviceCleanupStore
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_reservation_store import SlotReservationStore
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryTenant, seed_owned_slot
from tests.test_farm_agent_tasks import FakeRunner
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    SLOT1_SERIAL,
    FakeFarm,
    FakePlatform,
    _rental,
    _service,
)


class SyncWorker(VpsJobWorker):
    def enqueue_process(self, job_id: str) -> None:
        return None


def _health():
    return {
        "ok": True,
        "role": "farm",
        "mapped_slots": [1, 2, 21],
        "offline_slots": [],
        "slot_count": 3,
        "adb_online": 3,
    }


def _farm_svc(tmp_path: Path, farm=None):
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    reservations = SlotReservationStore(tmp_path / "slot_reservations.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    cleanup = DeviceCleanupStore(tmp_path / "cleanup.sqlite")
    tenant = MemoryTenant()
    worker = SyncWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=farm,
        poll_interval_seconds=3600.0,
        auth_store=tenant,
    )
    svc = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        reservation_store=reservations,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=_health,
        known_farm_slots={1, 2, 21},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
        cleanup_store=cleanup,
        farm_task_client=farm,
        slot_serial_map={21: "RFCNB056WEV", 1: "SER-1", 2: "SER-2"},
    )
    return svc, reservations, assign, cleanup, tenant


def test_lockdown_includes_s20_bloatware():
    assert "com.netflix.mediaclient" in LOCKDOWN_PACKAGES
    assert "com.netflix.partner.activation" in LOCKDOWN_PACKAGES
    assert "com.samsung.android.app.spage" in LOCKDOWN_PACKAGES
    assert "com.sec.android.app.kidshome" in LOCKDOWN_PACKAGES
    assert "com.samsung.android.game.gamehome" in LOCKDOWN_PACKAGES
    assert "com.samsung.android.game.gametools" in LOCKDOWN_PACKAGES
    assert "com.sec.android.gallery3d" not in LOCKDOWN_PACKAGES
    assert "device_about_phone_screenshot" in SUPPORTED_TASK_TYPES
    assert "airplane_mode" in SUPPORTED_TASK_TYPES


def test_preferred_slots_default_and_sort():
    assert parse_preferred_next_slot_ids(None) == (21,)
    assert parse_preferred_next_slot_ids("21") == (21,)
    assert parse_preferred_next_slot_ids("21,3") == (21, 3)
    assert sort_available_bays([1, 2, 21]) == [21, 1, 2]


def test_available_list_puts_bay_21_first(tmp_path: Path):
    svc, *_ = _farm_svc(tmp_path)
    result = svc.list_available_slots()
    assert result.http_status == 200
    bays = [row["bay"] for row in result.body["available"]]
    assert bays[0] == 21
    assert set(bays) == {1, 2, 21}


def test_force_available_clears_holds(tmp_path: Path):
    svc, reservations, assign, cleanup, _tenant = _farm_svc(tmp_path)
    rental = "00000000-0000-4000-8000-000000000021"
    reservations.claim(21, rental)
    cleanup.mark_required(21, rental, detail="test")
    cleanup.mark_disabled(21, rental)
    result = svc.force_available(21)
    assert result.http_status == 200
    assert result.body["available"] is True
    assert reservations.get(21) is None
    assert cleanup.is_required(21) is False
    assert cleanup.is_disabled(21) is False
    available = {row["bay"] for row in svc.list_available_slots().body["available"]}
    assert 21 in available


def test_disable_rental_blocks_remote_access(tmp_path: Path):
    farm = FakeFarm()
    platform = FakePlatform(registered={SLOT1_SERIAL})
    cleanup = DeviceCleanupStore(tmp_path / "ra-cleanup.sqlite")
    tenant = MemoryTenant()
    service, _store, _clock = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=farm,
        allowed=(1, 2),
        workspace_map={1: "ws-1", 2: "ws-2"},
    )
    service._cleanup = cleanup
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    cleanup.mark_disabled(1, rental)
    service.terminate_slot_sessions(1)
    denied = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert denied.http_status == 403
    assert denied.body["error"] == "rental_disabled"
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "http_status", 200) == 403


def test_farm_and_admin_routes_parse():
    reboot = parse_route("/farm/slots/21/reboot")
    assert reboot is not None and reboot.kind == "farm_reboot" and reboot.farm_bay == 21
    air = parse_route("/devices/21/airplane-mode")
    assert air is not None and air.kind == "devices_airplane_mode"
    about = parse_route("/admin/devices/21/about-phone-screenshot")
    assert about is not None and about.kind == "admin_device_about_phone"
    disable = parse_route("/farm/slots/21/disable-rental")
    assert disable is not None and disable.kind == "farm_disable_rental"
    enable = parse_route("/farm/slots/2/enable-rental")
    assert enable is not None and enable.kind == "farm_enable_rental" and enable.farm_bay == 2
    admin_enable = parse_route("/admin/devices/2/enable-rental")
    assert admin_enable is not None and admin_enable.kind == "admin_device_enable"
    admin_air = parse_route("/admin/devices/2/airplane-mode")
    assert admin_air is not None and admin_air.kind == "admin_device_airplane"
    live_air = parse_route("/admin/live-access/2/airplane-mode")
    assert live_air is not None and live_air.kind == "admin_live_airplane" and live_air.farm_bay == 2
    nested_air = parse_route("/admin/devices/2/live-access/airplane-mode")
    assert nested_air is not None and nested_air.kind == "admin_live_airplane"
    admin_force = parse_route("/admin/devices/2/force-available")
    assert admin_force is not None and admin_force.kind == "admin_device_force_available"
    admin_cleanup = parse_route("/admin/devices/2/cleanup-verified")
    assert admin_cleanup is not None and admin_cleanup.kind == "admin_device_cleanup_verified"
    admin_clean = parse_route("/admin/slots/21/clean")
    assert admin_clean is not None and admin_clean.kind == "admin_slot_clean" and admin_clean.farm_bay == 21


def test_disable_rental_turns_airplane_on_enable_clears_lock(tmp_path: Path):
    farm = FakeFarm()
    svc, _reservations, _assign, cleanup, _tenant = _farm_svc(tmp_path, farm=farm)
    disabled = svc.disable_rental(2)
    assert disabled.http_status == 200
    assert cleanup.is_disabled(2) is True
    assert disabled.body["rental_disabled"] is True
    air_on = [t for t in farm.tasks if t["type"] == "airplane_mode"]
    assert air_on
    assert air_on[0]["payload"]["enabled"] is True
    assert air_on[0]["payload"]["state"] == "on"
    enabled = svc.enable_rental(2)
    assert enabled.http_status == 200
    assert cleanup.is_disabled(2) is False
    assert enabled.body["rental_disabled"] is False
    air_off = [t for t in farm.tasks if t["type"] == "airplane_mode"][-1]
    assert air_off["payload"]["enabled"] is False
    assert air_off["payload"]["state"] == "off"


def test_airplane_accepts_enabled_flag_and_returns_enabled(tmp_path: Path):
    farm = FakeFarm()
    svc, *_ = _farm_svc(tmp_path, farm=farm)
    on = svc.run_airplane_mode(2, {"enabled": True})
    assert on.http_status == 200
    assert on.body["ok"] is True
    assert on.body["enabled"] is True
    off = svc.run_airplane_mode(2, {"enabled": False})
    assert off.body["ok"] is True
    assert off.body["enabled"] is False


def test_airplane_farm_task_uses_settings_and_broadcast():
    runner = FakeRunner()
    result = run_airplane_mode(
        adb_path="adb",
        slot_map={21: "RFCNB056WEV"},
        request=FarmTaskRequest(
            job_id="j",
            task_type="airplane_mode",
            farm_slot_id=21,
            payload={"state": "on"},
        ),
        command_runner=runner,
    )
    assert result.ok is True
    assert (result.details or {}).get("enabled") is True
    assert (result.details or {}).get("state") == "on"
    joined = " ".join(" ".join(args) for _s, args in runner.calls)
    assert "settings put global airplane_mode_on 1" in joined
    assert "android.intent.action.AIRPLANE_MODE" in joined
    assert "--ez state true" in joined
    assert "settings get global airplane_mode_on" in joined
    assert "cmd connectivity airplane-mode enable" in joined


def test_parse_airplane_state():
    from application.device_airplane_mode import requested_airplane_state

    assert parse_airplane_state("enabled") == "on"
    assert parse_airplane_state("disabled") == "off"
    assert parse_airplane_state("1") == "on"
    assert parse_airplane_state("0") == "off"
    assert requested_airplane_state({"enabled": True}) == "on"
    assert requested_airplane_state({"enabled": False}) == "off"
    assert requested_airplane_state({"state": "on"}) == "on"


def test_farm_rental_disabled_file(tmp_path: Path):
    from application.device_airplane_mode import farm_slot_rental_disabled, persist_farm_rental_disabled

    path = tmp_path / "disabled.json"
    persist_farm_rental_disabled(2, True, path=path)
    assert farm_slot_rental_disabled(2, path=path) is True
    persist_farm_rental_disabled(2, False, path=path)
    assert farm_slot_rental_disabled(2, path=path) is False


def test_end_rental_runs_device_data_cleanup(tmp_path: Path):
    farm = FakeFarm()
    svc, reservations, _assign, cleanup, tenant = _farm_svc(tmp_path, farm=farm)
    rental = "00000000-0000-4000-8000-000000000099"
    seed_owned_slot(tenant, bay=21, rental_id=rental, user_id=CUSTOMER_A)
    reservations.claim(21, rental)
    result = svc.end_rental(rental, explicit=True)
    assert result.http_status == 200
    types = [t["type"] for t in farm.tasks]
    assert "setup_session_safe_cleanup" in types
    assert "device_data_cleanup" in types
    assert cleanup.is_required(21) is True
    assert 21 not in {row["bay"] for row in svc.list_available_slots().body["available"]}


def test_about_phone_and_reboot_farm_ops(tmp_path: Path):
    farm = FakeFarm()
    svc, *_ = _farm_svc(tmp_path, farm=farm)
    air = svc.run_airplane_mode(21, {"state": "off"})
    assert air.http_status == 200
    about = svc.about_phone_screenshot(21)
    assert about.http_status == 200
    reboot = svc.reboot_slot(21)
    assert reboot.http_status == 200
    assert reboot.body["rebooted"] is True
    types = [t["type"] for t in farm.tasks]
    assert "airplane_mode" in types
    assert "device_about_phone_screenshot" in types
    assert "reboot" in types
    assert "device_screen_prepare" in types
