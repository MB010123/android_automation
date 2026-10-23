"""Farm Agent ADB data cleanup and VPS auto-cleanup identity payloads."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.device_data_cleanup_farm_task import (
    LOCKDOWN_PACKAGES,
    PRESERVED_PACKAGES,
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

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        self.calls.append((serial, arguments))
        joined = " ".join(arguments)
        if arguments == ["get-state"]:
            return AdbCommandResult(stdout=self._state, stderr="")
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
    assert details["cleared"] == ["files", "call_log", "contacts", "sms_mms", "browsers"]
    joined = [" ".join(args) for _serial, args in runner.calls]
    assert any("rm -rf" in item and "/sdcard/Pictures/*" in item for item in joined)
    assert any("MEDIA_SCANNER_SCAN_FILE" in item and "file:///sdcard/Pictures" in item for item in joined)
    assert any("MEDIA_SCANNER_SCAN_FILE" in item and "file:///sdcard/DCIM" in item for item in joined)
    assert any("content://call_log/calls" in item for item in joined)
    assert any("pm clear com.android.providers.contacts" in item for item in joined)
    assert any("content://sms" in item for item in joined)
    assert any("KEYCODE_HOME" in item for item in joined)
    assert all("reboot" not in item for item in joined)
    assert all("euicc" not in item.lower() for item in joined)
    assert all("lpa" not in item.lower() for item in joined)
    for package in LOCKDOWN_PACKAGES:
        assert any(f"pm disable-user --user 0 {package}" in item for item in joined)
    for package in PRESERVED_PACKAGES:
        assert all(package not in item or "disable-user" not in item for item in joined)


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


def test_cleanup_required_file_wipe_failure_fails_task():
    result = run_device_data_cleanup(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=_request(),
        command_runner=CleanupRunner(fail_on="rm -rf"),
    )
    assert result.ok is False
    assert result.error == "cleanup_adb_failed"
    assert result.details["rebooted"] is False
    assert result.details["esim_preserved"] is True


class MockCleanupFarm:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def run_task(self, **kwargs: object) -> FarmTaskResponse:
        self.calls.append(dict(kwargs))
        if self.fail:
            return FarmTaskResponse(ok=False, http_status=422, body={}, error="cleanup_adb_failed")
        return FarmTaskResponse(
            ok=True,
            http_status=200,
            body={
                "ok": True,
                "details": {
                    "slot_id": kwargs.get("farm_slot_id"),
                    "serial": S20_SERIAL,
                    "status": "cleanup_completed",
                    "esim_preserved": True,
                    "rebooted": False,
                    "cleared": ["files", "call_log", "contacts", "sms_mms", "browsers"],
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
        "cleared": ["files", "call_log", "contacts", "sms_mms", "browsers"],
        "lockdown_applied": True,
    }
    assert farm.calls[0]["task_type"] == "device_data_cleanup"
    assert farm.calls[0]["farm_slot_id"] == 21
    assert farm.calls[0]["payload"] == {"lockdown": True}
    assert "serial" not in farm.calls[0]["payload"]


def test_vps_auto_cleanup_serial_mismatch(tmp_path: Path):
    svc, farm = _farm_svc(tmp_path)
    result = svc.run_device_data_cleanup(21, {"serial": "WRONG"})
    assert result.http_status == 409
    assert result.body["error"] == "serial_mismatch"
    assert farm.calls == []


def test_cleanup_verified_fail_closed_keeps_hold(tmp_path: Path):
    svc, _farm = _farm_svc(tmp_path, farm=MockCleanupFarm(fail=True))
    rental = str(uuid.uuid4())
    svc._cleanup.mark_required(21, rental, detail="awaiting_operator")
    verified = svc.verify_cleanup(21)
    assert verified.http_status == 422
    assert svc._cleanup.is_required(21) is True
    assert verified.body["status"] == "cleanup_failed"
    assert verified.body["slot_id"] == 21
    assert verified.body["serial"] == S20_SERIAL
    assert verified.body["esim_preserved"] is True
    assert verified.body["rebooted"] is False


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
