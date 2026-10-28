"""Farm Agent ADB data cleanup and VPS auto-cleanup identity payloads."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.device_data_cleanup_farm_task import (
    ACCOUNT_CLEAR_PACKAGES,
    FACTORY_RESET_COMMANDS,
    LAUNCHER_CLEAR_PACKAGES,
    LOCKDOWN_PACKAGES,
    PRESERVED_PACKAGES,
    parse_preserve_esim,
    run_device_data_cleanup,
)
from application.farm_agent_tasks import parse_farm_task_body
from application.farm_task_types import FarmTaskRequest, SUPPORTED_TASK_TYPES
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from application.vps_lovable_routes import parse_route
from infrastructure.adb_companion import AdbCommandError, AdbCommandResult, AdbCommandRunner
from infrastructure.device_cleanup_store import DeviceCleanupStore
from infrastructure.farm_task_client import FarmTaskResponse
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_reservation_store import SlotReservationStore
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryTenant
from tools.farm_agent_status_server import build_farm_status

S20_SERIAL = "RFCNB056WEV"
PIXEL_SERIAL = "SERIAL-SLOT1-TEST"


class CleanupRunner(AdbCommandRunner):
    def __init__(self, state: str = "device", fail_on: str | None = None) -> None:
        super().__init__()
        self._state = state
        self.fail_on = fail_on
        self.calls: list[tuple[str, list[str]]] = []
        self.dumpsys_activity = ""

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        self.calls.append((serial, arguments))
        joined = " ".join(arguments)
        if arguments == ["get-state"]:
            return AdbCommandResult(stdout=self._state, stderr="")
        if arguments == ["wait-for-device"] or arguments[:1] == ["reboot"]:
            return AdbCommandResult(stdout="", stderr="")
        if arguments[:2] == ["shell", "getprop"] and "sys.boot_completed" in arguments:
            return AdbCommandResult(stdout="1", stderr="")
        if tuple(arguments[:3]) in {
            ("shell", "dumpsys", "window"),
            ("shell", "dumpsys", "activity"),
        }:
            return AdbCommandResult(stdout=self.dumpsys_activity, stderr="")
        if self.fail_on and self.fail_on in joined:
            raise AdbCommandError("injected failure")
        if arguments[:2] == ["shell", "pm"] and arguments[2] == "clear" and arguments[3].endswith(".missing"):
            raise AdbCommandError("package not found")
        return AdbCommandResult(stdout="", stderr="")


def _request(**payload: object) -> FarmTaskRequest:
    return FarmTaskRequest(
        job_id="cleanup-job",
        task_type="device_data_cleanup",
        farm_slot_id=21,
        payload=dict(payload),
    )


def test_device_data_cleanup_is_supported():
    assert "device_data_cleanup" in SUPPORTED_TASK_TYPES


def test_parse_allows_serial_only_on_cleanup_task():
    request = parse_farm_task_body(
        {
            "job_id": "j21",
            "type": "device_data_cleanup",
            "farm_slot_id": 21,
            "payload": {"serial": S20_SERIAL, "lockdown": True},
        }
    )
    assert request.payload["serial"] == S20_SERIAL
    with pytest.raises(ValueError, match="not allowed"):
        parse_farm_task_body(
            {
                "job_id": "j1",
                "type": "reboot",
                "farm_slot_id": 1,
                "payload": {"serial": S20_SERIAL},
            }
        )


def test_cleanup_wipes_files_media_contacts_sms_no_reboot(tmp_path: Path):
    del tmp_path
    runner = CleanupRunner()
    result = run_device_data_cleanup(
        adb_path="adb",
        slot_map={21: S20_SERIAL, 1: PIXEL_SERIAL},
        request=_request(),
        command_runner=runner,
    )
    assert result.ok is True
    assert result.http_status == 200
    details = result.details or {}
    assert details["slot_id"] == 21
    assert details["serial"] == S20_SERIAL
    assert details["status"] == "cleanup_completed"
    assert details["esim_preserved"] is True
    assert details["rebooted"] is False
    assert details["lockdown_applied"] is True
    assert details["cleared"] == [
        "files",
        "call_log",
        "contacts",
        "sms_mms",
        "browsers",
        "accounts",
        "launchers",
        "esim_qr",
    ]
    assert details["warnings"] == []
    assert runner.calls[0][1] == ["get-state"]
    script = next(
        args[1]
        for _serial, args in runner.calls
        if args[:1] == ["shell"] and "rm -rf /sdcard/Pictures; true" in (args[1] if len(args) > 1 else "")
    )
    assert "rm -rf /sdcard/Pictures; true" in script
    assert "mkdir -p /sdcard/Pictures; true" in script
    assert "rm -f /sdcard/Pictures/esim_qr.png; true" in script
    assert "rm -f /sdcard/Download/esim_qr.png; true" in script
    assert "rm -f /sdcard/DCIM/esim_qr.png; true" in script
    assert "rm -f /sdcard/DCIM/Camera/esim_qr.png; true" in script
    assert "rm -rf /sdcard/DCIM; true" in script
    assert "rm -rf /sdcard/DCIM/Camera; true" in script
    assert "*" not in script
    assert "sh -c" not in script
    assert "MEDIA_SCANNER_SCAN_FILE" in script and "file:///sdcard/Pictures" in script
    assert "file:///sdcard/DCIM" in script
    assert "file:///sdcard/DCIM/Camera" in script
    assert "MEDIA_MOUNTED" in script and "file:///sdcard" in script
    assert "content://call_log/calls" in script
    assert "pm clear com.android.providers.contacts" in script
    assert "content://sms" in script
    assert "KEYCODE_WAKEUP" in script
    assert "dismiss-keyguard" in script
    assert "stay_on_while_plugged_in 7" in script
    assert "KEYCODE_HOME" in script
    assert "reboot" not in script
    assert "euicc" not in script.lower()
    assert "lpa" not in script.lower()
    for package in LOCKDOWN_PACKAGES:
        assert f"pm disable-user --user 0 {package}" in script
    for package in (
        "com.sec.android.app.camera",
        "com.google.android.GoogleCamera",
        "com.android.camera2",
        "com.android.camera",
    ):
        assert f"pm disable-user --user 0 {package}" in script
        assert f"pm enable {package}" not in script
    assert "media volume --stream 3 --set 0" in script
    assert "cmd sensor_privacy enable 0 1" in script
    assert "cmd sensor_privacy enable 0 2" in script
    assert "cmd flashlight disable" in script
    assert "dpm set-user-restriction no_adjust_volume true" in script
    assert "dpm set-user-restriction no_camera true" in script
    assert "dpm set-user-restriction no_unmute_microphone true" in script
    for package in PRESERVED_PACKAGES:
        assert f"disable-user --user 0 {package}" not in script
    for package in ACCOUNT_CLEAR_PACKAGES:
        assert f"pm clear {package}" in script
    for package in LAUNCHER_CLEAR_PACKAGES:
        assert f"pm clear {package}" in script
    assert "WIPE_ESIMS" not in script
    assert "FACTORY_RESET" not in script
    assert "MASTER_CLEAR" not in script


def test_cleanup_lockdown_false_skips_disable():
    runner = CleanupRunner()
    result = run_device_data_cleanup(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=_request(lockdown=False),
        command_runner=runner,
    )
    assert result.ok is True
    assert result.details["lockdown_applied"] is False
    joined = [" ".join(args) for _serial, args in runner.calls]
    assert all("disable-user" not in item for item in joined)


def test_cleanup_serial_mismatch_and_optional_packages():
    mismatch = run_device_data_cleanup(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=_request(serial="WRONG"),
        command_runner=CleanupRunner(),
    )
    assert mismatch.ok is False
    assert mismatch.http_status == 409
    assert mismatch.error == "serial_mismatch"
    runner = CleanupRunner()
    result = run_device_data_cleanup(
        adb_path="adb",
        slot_map={1: PIXEL_SERIAL},
        request=FarmTaskRequest(
            job_id="j1",
            task_type="device_data_cleanup",
            farm_slot_id=1,
            payload={"serial": PIXEL_SERIAL},
        ),
        command_runner=runner,
    )
    assert result.ok is True
    assert result.details["serial"] == PIXEL_SERIAL


def test_cleanup_file_wipe_failure_is_warning():
    result = run_device_data_cleanup(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=_request(),
        command_runner=CleanupRunner(fail_on="rm -rf"),
    )
    assert result.ok is True
    assert result.http_status == 200
    assert result.error is None
    assert result.details["status"] == "cleanup_completed"
    assert result.details["rebooted"] is False
    assert result.details["esim_preserved"] is True
    assert any("cleanup_script" in item for item in result.details["warnings"])


def test_cleanup_offline_device_still_fails():
    result = run_device_data_cleanup(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=_request(),
        command_runner=CleanupRunner(state="offline"),
    )
    assert result.ok is False
    assert result.http_status == 409
    assert result.error == "device_offline"


class MockCleanupFarm:
    def __init__(
        self,
        *,
        fail: bool = False,
        error: str = "cleanup_adb_failed",
        http_status: int = 422,
    ) -> None:
        self.fail = fail
        self.error = error
        self.http_status = http_status
        self.calls: list[dict] = []

    def run_task(self, **kwargs: object) -> FarmTaskResponse:
        self.calls.append(dict(kwargs))
        if self.fail:
            return FarmTaskResponse(ok=False, http_status=self.http_status, body={}, error=self.error)
        payload = kwargs.get("payload") if isinstance(kwargs.get("payload"), dict) else {}
        preserve = parse_preserve_esim(payload if isinstance(payload, dict) else {})
        return FarmTaskResponse(
            ok=True,
            http_status=200,
            body={
                "ok": True,
                "details": {
                    "slot_id": kwargs.get("farm_slot_id"),
                    "serial": S20_SERIAL,
                    "status": "cleanup_completed",
                    "esim_preserved": preserve,
                    "rebooted": not preserve,
                    "cleared": [
                        "files",
                        "call_log",
                        "contacts",
                        "sms_mms",
                        "browsers",
                        "accounts",
                        "launchers",
                    ],
                    "lockdown_applied": True,
                },
            },
        )


class SyncWorker(VpsJobWorker):
    def enqueue_process(self, job_id: str) -> None:
        return None


def _farm_svc(tmp_path: Path, *, farm: MockCleanupFarm | None = None) -> tuple[VpsFarmManagementService, MockCleanupFarm]:
    farm = farm or MockCleanupFarm()
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    reservations = SlotReservationStore(tmp_path / "reservations.sqlite")
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
        farm_status_fetcher=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": [1, 21],
            "slot_count": 2,
            "adb_online": 2,
        },
        known_farm_slots={1, 21},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
        esim_url_prefixes=("https://example.test/",),
        cleanup_store=cleanup,
        farm_task_client=farm,
        slot_serial_map={1: PIXEL_SERIAL, 21: S20_SERIAL},
    )
    return svc, farm


def test_vps_auto_cleanup_returns_slot_and_serial(tmp_path: Path):
    svc, farm = _farm_svc(tmp_path)
    result = svc.run_device_data_cleanup(21, {"lockdown": True}, require_farm=True)
    assert result.http_status == 200, result.body
    assert result.body == {
        "ok": True,
        "slot_id": 21,
        "serial": S20_SERIAL,
        "status": "cleanup_completed",
        "esim_preserved": True,
        "rebooted": False,
        "cleared": [
            "files",
            "call_log",
            "contacts",
            "sms_mms",
            "browsers",
            "accounts",
            "launchers",
        ],
        "lockdown_applied": True,
        "warnings": [],
        "rental_preserved": False,
    }
    assert farm.calls[0]["task_type"] == "device_data_cleanup"
    assert farm.calls[0]["farm_slot_id"] == 21
    assert farm.calls[0]["payload"] == {"lockdown": True, "preserve_esim": True}
    assert "serial" not in farm.calls[0]["payload"]


def test_vps_auto_cleanup_serial_mismatch(tmp_path: Path):
    svc, farm = _farm_svc(tmp_path)
    result = svc.run_device_data_cleanup(21, {"serial": "WRONG"})
    assert result.http_status == 409
    assert result.body["error"] == "serial_mismatch"
    assert farm.calls == []


def test_cleanup_verified_releases_hold_when_adb_emits_warnings(tmp_path: Path):
    svc, _farm = _farm_svc(tmp_path, farm=MockCleanupFarm(fail=True))
    rental = str(uuid.uuid4())
    svc._cleanup.mark_required(21, rental, detail="awaiting_operator")
    verified = svc.verify_cleanup(21)
    assert verified.http_status == 200, verified.body
    assert verified.body["status"] == "cleanup_completed"
    assert verified.body["ok"] is True
    assert verified.body["cleanup"] == "cleared"
    assert "cleanup_adb_failed" in verified.body["warnings"]
    assert svc._cleanup.is_required(21) is False


def test_cleanup_verified_releases_hold_on_farm_error(tmp_path: Path):
    svc, _farm = _farm_svc(
        tmp_path,
        farm=MockCleanupFarm(fail=True, error="device_offline", http_status=409),
    )
    rental = str(uuid.uuid4())
    svc._cleanup.mark_required(21, rental, detail="awaiting_operator")
    verified = svc.verify_cleanup(21)
    assert verified.http_status == 200, verified.body
    assert verified.body["cleanup_warning"] == "device_offline"
    assert verified.body["status"] == "cleanup_completed"
    assert verified.body["available"] is True
    assert svc._cleanup.is_required(21) is False


def test_cleanup_verified_success_clears_hold(tmp_path: Path):
    svc, farm = _farm_svc(tmp_path)
    rental = str(uuid.uuid4())
    svc._cleanup.mark_required(21, rental, detail="awaiting_operator")
    verified = svc.verify_cleanup(21)
    assert verified.http_status == 200, verified.body
    assert verified.body["cleanup"] == "cleared"
    assert verified.body["slot_id"] == 21
    assert verified.body["serial"] == S20_SERIAL
    assert verified.body["status"] == "cleanup_completed"
    assert svc._cleanup.is_required(21) is False
    assert farm.calls[0]["task_type"] == "device_data_cleanup"


def test_status_and_available_include_serial(tmp_path: Path):
    svc, _farm = _farm_svc(tmp_path)
    available = svc.list_available_slots()
    slot21 = next(item for item in available.body["available"] if item["bay"] == 21)
    assert slot21["serial"] == S20_SERIAL
    from infrastructure.slot_public_id import public_id_for_farm_slot

    status = svc.get_slot_status(public_id_for_farm_slot(21))
    assert status.body["bay"] == 21
    assert status.body["serial"] == S20_SERIAL


def test_farm_health_lists_slot_id_and_serial():
    body = build_farm_status(adb_path="adb-missing-for-test", slot_map={21: S20_SERIAL, 1: PIXEL_SERIAL})
    by_id = {item["slot_id"]: item["serial"] for item in body["slots"]}
    assert by_id[21] == S20_SERIAL
    assert by_id[1] == PIXEL_SERIAL


def test_auto_cleanup_route_parses_slot_21():
    parsed = parse_route("/farm/slots/21/auto-cleanup")
    assert parsed is not None
    assert parsed.kind == "farm_auto_cleanup"
    assert parsed.farm_bay == 21
    listed = parse_route("/farm/slots")
    assert listed is not None
    assert listed.kind == "farm_slots"


def test_access_mode_routes_parse():
    query = parse_route("/farm/access-mode")
    assert query is not None and query.kind == "farm_access_mode"
    farm = parse_route("/farm/slots/21/access-mode")
    assert farm is not None and farm.kind == "farm_access_mode_slot" and farm.farm_bay == 21
    admin = parse_route("/admin/devices/21/access-mode")
    assert admin is not None and admin.kind == "admin_device_access_mode" and admin.farm_bay == 21


def test_parse_preserve_esim_defaults_true():
    assert parse_preserve_esim(None) is True
    assert parse_preserve_esim({}) is True
    assert parse_preserve_esim({"preserve_esim": True}) is True
    assert parse_preserve_esim({"preserve_esim": "false"}) is False
    assert parse_preserve_esim({"preserve_esim": 0}) is False


def test_keep_esim_cleanup_does_not_factory_reset():
    runner = CleanupRunner()
    result = run_device_data_cleanup(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=_request(preserve_esim=True),
        command_runner=runner,
    )
    assert result.ok is True
    assert result.details["esim_preserved"] is True
    assert result.details["rebooted"] is False
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert "FACTORY_RESET" not in joined
    assert "reboot recovery" not in joined
    assert "euicc" not in joined.lower()


def test_full_wipe_factory_resets_and_reboots_recovery():
    runner = CleanupRunner()
    result = run_device_data_cleanup(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=_request(preserve_esim=False),
        command_runner=runner,
        sleep_fn=lambda _s: None,
    )
    assert result.ok is True, (result.error, result.details)
    assert result.details["esim_preserved"] is False
    assert result.details["rebooted"] is True
    assert result.details["status"] == "cleanup_completed"
    assert any(args[:2] == ["reboot", "recovery"] for _s, args in runner.calls)
    scripts = " ".join(args[1] for _s, args in runner.calls if args and args[0] == "shell")
    for command in FACTORY_RESET_COMMANDS:
        assert command in scripts
    assert "WIPE_ESIMS" in scripts
    assert "EuiccManager" not in scripts
    assert "pm clear com.android.euicc" not in scripts
    assert ("RFCNB056WEV", ["wait-for-device"]) in runner.calls


def test_admin_clean_routes_parse():
    admin = parse_route("/admin/slots/21/clean")
    assert admin is not None and admin.kind == "admin_slot_clean" and admin.farm_bay == 21
    devices = parse_route("/admin/devices/21/clean")
    assert devices is not None and devices.kind == "admin_device_clean" and devices.farm_bay == 21
    farm = parse_route("/farm/slots/21/clean")
    assert farm is not None and farm.kind == "farm_clean" and farm.farm_bay == 21


def test_admin_clean_runs_on_active_rental_and_keeps_assignment(tmp_path: Path):
    svc, farm = _farm_svc(tmp_path)
    rental = str(uuid.uuid4())
    assert svc._reservations.claim(21, rental) is True
    assert svc._assignments.claim(21, rental, "job-clean") is True
    result = svc.run_device_data_cleanup(21, {"preserve_esim": True}, require_farm=True)
    assert result.http_status == 200, result.body
    assert result.body["status"] == "cleanup_completed"
    assert result.body["esim_preserved"] is True
    assert result.body["rental_preserved"] is True
    assert result.body["rental_id"] == rental
    assert result.body["rebooted"] is False
    assert svc._reservations.is_reserved(21) is True
    assert svc._assignments.is_assigned(21) is True
    assert svc._reservations.get(21).rental_id == rental
    assert farm.calls[0]["payload"] == {"lockdown": True, "preserve_esim": True}


def test_vps_clean_slot_wipes_esim_when_requested(tmp_path: Path):
    svc, farm = _farm_svc(tmp_path)
    kept = svc.run_device_data_cleanup(21, {}, require_farm=True)
    assert kept.http_status == 200
    assert kept.body["esim_preserved"] is True
    assert kept.body["rebooted"] is False
    wiped = svc.run_device_data_cleanup(21, {"preserve_esim": False}, require_farm=True)
    assert wiped.http_status == 200, wiped.body
    assert wiped.body["esim_preserved"] is False
    assert wiped.body["rebooted"] is True
    assert farm.calls[-1]["payload"]["preserve_esim"] is False


def test_device_screen_prepare_resets_display_and_unlocks():
    from application.device_screen_prepare import run_device_screen_prepare

    runner = CleanupRunner()
    result = run_device_screen_prepare(
        adb_path="adb",
        slot_map={2: PIXEL_SERIAL, 21: S20_SERIAL},
        request=FarmTaskRequest(
            job_id="prep",
            task_type="device_screen_prepare",
            farm_slot_id=2,
            payload={},
        ),
        command_runner=runner,
    )
    assert result.ok is True
    assert result.details["status"] == "screen_prepared"
    scripts = " ".join(args[1] for _serial, args in runner.calls if args and args[0] == "shell")
    assert "wm size reset; true" in scripts
    assert "wm density reset; true" in scripts
    assert "stay_on_while_plugged_in 7; true" in scripts
    assert "locksettings set-disabled true; true" in scripts
    assert "KEYCODE_WAKEUP" in scripts
    assert "dismiss-keyguard" in scripts
    assert "input swipe 540 1800 540 400 300" in scripts
    assert "KEYCODE_HOME" in scripts
    assert "com.samsung.android.game.gamehome" not in scripts
    assert "pm disable-user --user 0 com.google.android.GoogleCamera" in scripts
    assert "pm disable-user --user 0 com.sec.android.app.camera" in scripts
    assert "media volume --stream 3 --set 0" in scripts
    assert "cmd flashlight disable" in scripts
    joined_all = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert "/data/local/tmp/mobirent_lockdown_guard.sh" in joined_all
    assert "nohup sh /data/local/tmp/mobirent_lockdown_guard.sh" in joined_all


def test_device_screen_prepare_skip_home_omits_keycode_home():
    from application.device_screen_prepare import run_device_screen_prepare
    from application.samsung_one_ui import SAMSUNG_SIM_MGR_COMPONENT

    runner = CleanupRunner()
    runner.dumpsys_activity = (
        "mCurrentFocus=Window{abc u0 com.sec.android.app.launcher/"
        "com.sec.android.app.launcher.Launcher}"
    )
    result = run_device_screen_prepare(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=FarmTaskRequest(
            job_id="prep-skip-home",
            task_type="device_screen_prepare",
            farm_slot_id=21,
            payload={"skip_home": True},
        ),
        command_runner=runner,
    )
    assert result.ok is True
    scripts = " ".join(args[1] for _serial, args in runner.calls if args and args[0] == "shell")
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert "KEYCODE_WAKEUP" in scripts
    assert "dismiss-keyguard" in scripts
    assert "KEYCODE_HOME" not in joined
    assert "input keyevent 3" not in joined
    assert "wm size" not in scripts
    assert "wm density" not in scripts
    assert "input swipe" not in scripts
    assert SAMSUNG_SIM_MGR_COMPONENT in joined
    assert "--activity-clear-top" in joined
    assert "input tap" not in joined
    assert "uiautomator" not in joined
    assert result.details["skip_home"] is True
    assert result.details["esim_setup_launched"] == "sim_manager"
    assert result.details["setup_ui_state"] == "awaiting_human_click"


def test_device_screen_prepare_skip_home_always_reopens_sim_manager():
    from application.device_screen_prepare import run_device_screen_prepare
    from application.samsung_one_ui import SAMSUNG_SIM_MGR_COMPONENT

    runner = CleanupRunner()
    runner.dumpsys_activity = (
        "mCurrentFocus=Window{abc u0 com.samsung.android.app.telephonyui/"
        ".netsettings.ui.simcardmanager.SimCardMgrActivity}"
    )
    result = run_device_screen_prepare(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=FarmTaskRequest(
            job_id="prep-already-open",
            task_type="device_screen_prepare",
            farm_slot_id=21,
            payload={"skip_home": True, "esim_profile_present": True},
        ),
        command_runner=runner,
    )
    assert result.ok is True
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert SAMSUNG_SIM_MGR_COMPONENT in joined
    assert "KEYCODE_HOME" not in joined
    assert "input tap" not in joined
    assert result.details["esim_setup_launched"] == "sim_manager"


def test_device_screen_prepare_skip_home_reopens_pixel_sims_from_launcher():
    from application.device_screen_prepare import run_device_screen_prepare

    runner = CleanupRunner()
    runner.dumpsys_activity = (
        "mCurrentFocus=Window{abc u0 com.google.android.apps.nexuslauncher/"
        ".NexusLauncherActivity}"
    )
    result = run_device_screen_prepare(
        adb_path="adb",
        slot_map={1: PIXEL_SERIAL},
        request=FarmTaskRequest(
            job_id="prep-pixel-home",
            task_type="device_screen_prepare",
            farm_slot_id=1,
            payload={
                "setup_ui_state": "awaiting_human_click",
                "esim_profile_present": True,
            },
        ),
        command_runner=runner,
    )
    assert result.ok is True
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert "android.settings.WIRELESS_SETTINGS" in joined
    assert "--activity-clear-top" in joined
    assert "android.telephony.euicc.action.MANAGE_EMBEDDED_SUBSCRIPTIONS" not in joined
    assert "KEYCODE_HOME" not in joined
    assert "input tap" not in joined
    assert "uiautomator" not in joined
    assert result.details["esim_setup_launched"] == "android.settings.WIRELESS_SETTINGS"
    assert result.details["setup_ui_state"] == "awaiting_human_click"


def test_device_screen_prepare_open_esim_settings_launches_once():
    from application.device_screen_prepare import run_device_screen_prepare

    runner = CleanupRunner()
    result = run_device_screen_prepare(
        adb_path="adb",
        slot_map={1: PIXEL_SERIAL},
        request=FarmTaskRequest(
            job_id="prep-open-once",
            task_type="device_screen_prepare",
            farm_slot_id=1,
            payload={"skip_home": True, "open_esim_settings": True},
        ),
        command_runner=runner,
    )
    assert result.ok is True
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert "android.settings.WIRELESS_SETTINGS" in joined
    assert "--activity-clear-top" in joined
    assert "dumpsys" not in joined
    assert "input tap" not in joined
    assert result.details["esim_setup_launched"] == "android.settings.WIRELESS_SETTINGS"
    assert result.details["setup_ui_state"] == "awaiting_human_click"


def test_device_screen_prepare_s20_uses_fhd_not_physical_wqhd():
    from application.device_screen_prepare import run_device_screen_prepare

    runner = CleanupRunner()
    result = run_device_screen_prepare(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=FarmTaskRequest(
            job_id="prep21",
            task_type="device_screen_prepare",
            farm_slot_id=21,
            payload={},
        ),
        command_runner=runner,
    )
    assert result.ok is True
    scripts = " ".join(args[1] for _serial, args in runner.calls if args and args[0] == "shell")
    assert "wm size 1080x2400; true" in scripts
    assert "wm density 420; true" in scripts
    assert "wm size reset; true" not in scripts
    assert "wm density reset; true" not in scripts
    assert "pm disable-user --user 0 com.samsung.android.game.gamehome" in scripts
    assert "pm disable-user --user 0 com.samsung.android.game.gametools" in scripts
    assert "pm disable-user --user 0 com.sec.android.app.camera" in scripts
    assert "pm disable-user --user 0 com.google.android.GoogleCamera" in scripts
    assert "media volume --stream 3 --set 0" in scripts


def test_device_screen_prepare_wait_boot_then_unlock():
    from application.device_screen_prepare import run_device_screen_prepare

    class BootRunner(CleanupRunner):
        def run(self, serial: str, arguments: list[str]):
            self.calls.append((serial, arguments))
            if arguments == ["get-state"]:
                return AdbCommandResult(stdout="device", stderr="")
            if arguments == ["wait-for-device"]:
                return AdbCommandResult(stdout="", stderr="")
            if arguments[:3] == ["shell", "getprop", "sys.boot_completed"]:
                return AdbCommandResult(stdout="1", stderr="")
            return AdbCommandResult(stdout="", stderr="")

    runner = BootRunner()
    slept: list[float] = []
    result = run_device_screen_prepare(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=FarmTaskRequest(
            job_id="prep21",
            task_type="device_screen_prepare",
            farm_slot_id=21,
            payload={"wait_boot": True},
        ),
        command_runner=runner,
        sleep_fn=slept.append,
    )
    assert result.ok is True
    assert ("RFCNB056WEV", ["wait-for-device"]) in runner.calls
    assert any(args[:3] == ["shell", "getprop", "sys.boot_completed"] for _s, args in runner.calls)
    assert slept == []


def test_mjpeg_sniffs_samsung_boundary_and_wraps_raw_jpeg():
    from tools.vps_backend_server import mjpeg_content_type, mjpeg_prefixed_chunk

    declared = "multipart/x-mixed-replace"
    body = b"--GadsBoundary\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xff"
    assert mjpeg_content_type(declared, body) == "multipart/x-mixed-replace; boundary=GadsBoundary"
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 8 + b"\xff\xd9"
    ctype, prefixed = mjpeg_prefixed_chunk("image/octet-stream", jpeg)
    assert ctype == "multipart/x-mixed-replace; boundary=frame"
    assert prefixed.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8")
    kept = "multipart/x-mixed-replace; boundary=frame"
    assert mjpeg_content_type(kept, body) == kept
