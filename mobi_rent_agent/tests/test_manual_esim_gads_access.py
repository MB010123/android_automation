"""Manual eSIM + GADS: requires_manual_action is not phone-busy."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_agent_tasks import FarmTaskRequest, execute_farm_task
from application.farm_task_executor import FarmTaskExecutorDeps
from application.vps_api_contract import ERROR_MESSAGES, error_body, failure_class, provisioning_phase
from application.vps_slot_state import derive_slot_state
from domain.remote_access import RemoteDeviceStatus
from infrastructure.gads_remote_access import GadsHubClient, GadsRemoteAccessPlatform
from infrastructure.vps_job_store import VpsJobRecord
from tests.fakes_supabase import MemoryTenant
from tests.test_farm_agent_tasks import FakeResolver, FakeRunner, _config
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeFarm,
    FakePlatform,
    SLOT1_SERIAL,
    _Resp,
    _SseResp,
    _gads_multi,
    _rental,
    _service,
)


def test_requires_manual_action_is_not_phone_busy():
    assert "phone_operation_busy" not in ERROR_MESSAGES
    assert "The phone is busy with another operation" not in ERROR_MESSAGES.values()
    state = derive_slot_state(
        is_assigned=True,
        active_job_type=None,
        last_assign_phase="requires_manual_action",
        heartbeat_fresh=True,
        adb_online=True,
    )
    assert state == "requires_manual_action"
    assert state != "busy"
    record = VpsJobRecord(
        job_id="j1",
        type="assign",
        farm_slot_id=1,
        status="failed",
        progress=0,
        request_payload={},
        result_payload={"message": "human Settings/LPA required; automatic eSIM provision is disabled"},
        error="provisioning_failed",
        idempotency_key=None,
        created_at=1.0,
        updated_at=1.0,
        started_at=1.0,
        completed_at=1.0,
    )
    assert failure_class(record) == "requires_manual_action"
    assert provisioning_phase(record) == "requires_manual_action"
    busy = error_body("remote_access_busy")
    assert busy["error"] == "remote_access_busy"
    assert "phone is busy with another operation" not in busy["message"].lower()


def test_online_phone_in_manual_action_can_use_gads(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.inspect_allowed = False
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    assert created.body.get("error") != "phone_operation_busy"
    stream = service.open_stream(CUSTOMER_A, rental)
    assert not hasattr(stream, "http_status")
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 40, "y": 80})
    swipe = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 10, "y": 20, "x2": 30, "y2": 40}
    )
    typed = service.control_session(CUSTOMER_A, rental, {"action": "type", "text": "ok"})
    back = service.control_session(CUSTOMER_A, rental, {"action": "back"})
    assert tap.http_status == 200 and swipe.http_status == 200
    assert typed.http_status == 200 and back.http_status == 200
    inspects = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects == []
    assert "phone_operation_busy" not in str(tap.body)
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["state"] == "online"
    assert status.body["busy"] is False


def test_restricted_controls_and_cross_rental_stay_blocked(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _gads_multi(tmp_path, tenant, farm=FakeFarm())
    rental_a = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=9, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201
    assert service.create_remote_access(CUSTOMER_B, None, rental_b).http_status == 201
    assert service.control_session(CUSTOMER_A, rental_a, {"action": "home"}).http_status == 200
    assert service.control_session(CUSTOMER_A, rental_a, {"action": "recents"}).http_status == 200
    shade = service.control_session(
        CUSTOMER_A, rental_a, {"action": "swipe", "x": 10, "y": 5, "x2": 10, "y2": 400}
    )
    assert shade.http_status == 403
    assert shade.body["error"] == "forbidden_control"
    assert service.control_session(CUSTOMER_B, rental_a, {"action": "tap", "x": 1, "y": 1}).http_status == 403
    assert service.open_stream(CUSTOMER_B, rental_a).http_status == 403
    stolen = service.create_remote_access(CUSTOMER_A, None, rental_b)
    assert stolen.http_status == 403


def test_real_conflict_is_remote_access_busy_not_phone_operation_busy(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=FakeFarm())
    first = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, first).http_status == 201
    import uuid

    second = str(uuid.uuid4())
    tenant.slots[1]["id"] = second
    tenant.slots[1]["rental_id"] = second
    tenant.slots[1]["user_id"] = CUSTOMER_B
    busy = service.create_remote_access(CUSTOMER_B, None, second)
    assert busy.http_status == 409
    assert busy.body["error"] == "remote_access_busy"
    assert busy.body["error"] != "phone_operation_busy"
    assert "phone is busy with another operation" not in busy.body["message"].lower()


def test_gads_in_use_stream_is_not_reported_busy():
    pub = RemoteDeviceStatus(
        slot_id=1,
        device_id=SLOT1_SERIAL,
        registered=True,
        online=True,
        available=False,
        in_use_by="rental-abc",
    ).to_public_dict()
    assert pub["state"] == "online"
    assert pub["busy"] is False
    assert pub["available"] is True

    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            if url.endswith("/authenticate"):
                return _Resp(200, {"success": True, "result": {"access_token": "admin-jwt"}})
            if url.endswith("/admin/devices"):
                return _Resp(
                    200,
                    {"success": True, "result": {"devices": [{"udid": SLOT1_SERIAL, "workspace_id": "ws"}]}},
                )
            raise AssertionError(url)

        def get(self, url, params=None, headers=None, timeout=None, stream=False):
            live = [
                {
                    "info": {"udid": SLOT1_SERIAL},
                    "connected": True,
                    "provider_state": "live",
                    "available": False,
                    "in_use": True,
                    "in_use_by": "rental-abc",
                }
            ]
            return _SseResp(f"data:{json.dumps(live)}")

    client = GadsHubClient("http://hub", admin_username="a", admin_password="b", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(client, workspace_id="ws", public_url="https://remote.example")
    status = platform.device_status(slot_id=1, device_id=SLOT1_SERIAL)
    assert status.online is True
    assert status.to_public_dict()["state"] == "online"
    assert status.to_public_dict()["busy"] is False


def test_farm_assign_does_not_auto_provision_or_call_euicc():
    source = (ROOT / "application" / "farm_task_executor.py").read_text(encoding="utf-8")
    assert "EuiccManager" not in source
    assert "provision_esim" not in source
    assert "provisioner.provision" not in source
    provisioner = type("P", (), {"provision": staticmethod(lambda *a, **k: (_ for _ in ()).throw(AssertionError("provision")))})()
    result = execute_farm_task(
        adb_path="adb",
        slot_map={1: "SERIAL-A"},
        request=FarmTaskRequest(
            job_id="job-1",
            task_type="assign",
            farm_slot_id=1,
            payload={"esim_qr_url": "https://example.com/qr.png"},
        ),
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(
            command_runner=FakeRunner(),
            provisioner=provisioner,  # type: ignore[arg-type]
            payload_resolver=FakeResolver(),
        ),
    )
    assert result.ok is False
    assert result.activation_code_sent is False
    assert "human Settings/LPA required" in (result.message or "")


def test_esim_becomes_active_only_after_confirmed_observation(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.inspect_allowed = False
    service, store, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(),
        farm=farm,
        voidfix_package="com.voidfix.app",
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    premature = service.complete_setup(CUSTOMER_A, rental)
    assert premature.http_status == 200, premature.body
    assert premature.body["setup_complete"] is False
    assert premature.body["remote_session"] == "closed"
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    farm.activation_details = {"verdict": "ACTIVATION_CONFIRMED"}
    observed = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert observed.http_status == 200
    assert store.get(rental).activation_observed == "confirmed"
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200
    assert done.body["ui_state"] == "phone_ready"
    assert done.body["activation_observed"] == "confirmed"
