"""GADS tap 404 falls back to Farm input; 5xx and success do not fake taps."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_agent_tasks import FarmTaskRequest, execute_farm_task
from application.farm_task_executor import FarmTaskExecutorDeps
from domain.remote_access import RemoteAccessPlatformError
from infrastructure.gads_remote_access import GadsHubClient, GadsRemoteAccessPlatform
from tests.fakes_supabase import MemoryTenant
from tests.test_farm_agent_tasks import FakeRunner, _config
from tests.test_esim_setup_mode import _slot2_service
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeFarm,
    FakePlatform,
    SLOT1_SERIAL,
    SLOT2_SERIAL,
    SLOT8_SERIAL,
    _Resp,
    _gads_multi,
    _rental,
    _service,
)

TAP = {"action": "tap", "x": 540, "y": 960}
SWIPE = {"action": "swipe", "x": 10, "y": 400, "x2": 10, "y2": 200}


def _input_tasks(farm: FakeFarm) -> list[dict]:
    return [t for t in farm.tasks if t["type"] == "setup_session_input"]


def test_gads_tap_404_falls_back_to_assigned_slot_farm_tap(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    platform.gads_tap_available = False
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    result = service.control_session(CUSTOMER_A, rental, TAP)
    assert result.http_status == 200
    assert result.body["ok"] is True
    assert result.body["forwarded"] is True
    assert any(c[0] == "tap" for c in platform.calls)
    inputs = _input_tasks(farm)
    assert len(inputs) == 1
    assert inputs[0]["slot"] == 1
    assert inputs[0]["payload"] == {"kind": "tap", "x": 540, "y": 960}


def test_gads_tap_5xx_does_not_fall_back_or_fake_success(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    platform.tap_error = "gads_tap_failed status=503"
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    result = service.control_session(CUSTOMER_A, rental, TAP)
    assert result.http_status == 502
    assert result.body["error"] == "gads_unavailable"
    assert result.body.get("ok") is False
    assert _input_tasks(farm) == []


def test_gads_tap_404_and_farm_fail_is_still_502(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    platform.gads_tap_available = False
    farm = FakeFarm()
    farm.fail_types.add("setup_session_input")
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    result = service.control_session(CUSTOMER_A, rental, TAP)
    assert result.http_status == 502
    assert result.body["error"] == "farm_unreachable"
    assert result.body.get("ok") is False


def test_gads_tap_200_never_calls_farm_input(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    result = service.control_session(CUSTOMER_A, rental, TAP)
    assert result.http_status == 200
    assert any(c[0] == "tap" and c[1]["x"] == 540 and c[1]["y"] == 960 for c in platform.calls)
    assert _input_tasks(farm) == []


def test_cross_rental_tap_does_not_target_another_slot(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT8_SERIAL})
    platform.online[SLOT8_SERIAL] = True
    farm = FakeFarm()
    service, _, _ = _gads_multi(tmp_path, tenant, platform=platform, farm=farm)
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=8, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201
    assert service.create_remote_access(CUSTOMER_B, None, rental_b).http_status == 201
    platform.gads_tap_available = False
    stolen = service.control_session(CUSTOMER_B, rental_a, TAP)
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    assert _input_tasks(farm) == []
    own = service.control_session(CUSTOMER_A, rental_a, TAP)
    assert own.http_status == 200
    inputs = _input_tasks(farm)
    assert len(inputs) == 1
    assert inputs[0]["slot"] == 1
    assert inputs[0]["payload"]["kind"] == "tap"
    denied_target = service.control_session(
        CUSTOMER_A, rental_a, {"action": "tap", "x": 1, "y": 1, "slot_id": 8, "serial": SLOT8_SERIAL}
    )
    assert denied_target.http_status == 403
    assert denied_target.body["error"] == "forbidden_control"
    assert [t["slot"] for t in _input_tasks(farm)] == [1]


def test_restrictions_inspect_fail_after_tap_still_200(tmp_path: Path):
    farm = FakeFarm()
    farm.fail_types.add("setup_session_inspect")
    platform = FakePlatform(registered={SLOT2_SERIAL})
    _tenant, platform, farm, service, _store, rental = _slot2_service(
        tmp_path, farm=farm, platform=platform
    )
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    result = service.control_session(CUSTOMER_A, rental, TAP)
    assert result.http_status == 200
    assert result.body["ok"] is True
    assert result.body["forwarded"] is True
    assert "restricted_destination" not in result.body
    assert any(c[0] == "tap" for c in platform.calls)
    assert _input_tasks(farm) == []


def test_swipe_404_still_falls_back_to_farm(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    platform.gads_swipe_available = False
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    result = service.control_session(CUSTOMER_A, rental, SWIPE)
    assert result.http_status == 200
    inputs = _input_tasks(farm)
    assert len(inputs) == 1
    assert inputs[0]["slot"] == 1
    assert inputs[0]["payload"]["kind"] == "swipe"
    assert inputs[0]["payload"]["x"] == 10
    assert inputs[0]["payload"]["y2"] == 200


def test_swipe_200_never_calls_farm_input(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    result = service.control_session(CUSTOMER_A, rental, SWIPE)
    assert result.http_status == 200
    assert any(c[0] == "swipe" for c in platform.calls)
    assert _input_tasks(farm) == []


def test_farm_unknown_kind_is_not_used_when_gads_tap_exists(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    result = service.control_session(CUSTOMER_A, rental, TAP)
    assert result.http_status == 200
    assert _input_tasks(farm) == []
    assert not any(t["payload"].get("kind") == "tap" for t in farm.tasks)


def test_gads_adapter_tap_404_is_missing_endpoint_not_auth():
    seen: list[tuple[str, str, dict | None]] = []

    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            if url.endswith("/authenticate"):
                return _Resp(200, {"success": True, "result": {"access_token": "jwt-user"}})
            seen.append((method, url, json))
            if url.endswith("/tap"):
                return _Resp(404, {"success": False})
            return _Resp(404, {"success": False})

    client = GadsHubClient("http://hub", admin_username="a", admin_password="b", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(
        client, workspace_id="ws", public_url="https://remote.example", clock=lambda: 1_000_000.0
    )
    kwargs = {
        "device_id": SLOT1_SERIAL,
        "platform_username": "rental-user",
        "platform_password": "secret",
    }
    assert platform.tap(**kwargs, x=540, y=960) is False
    assert any(url.endswith(f"/device/{SLOT1_SERIAL}/tap") for _method, url, _body in seen)
    assert seen[-1][2] == {"x": 540, "y": 960}


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_gads_adapter_tap_auth_and_5xx_still_raise(status: int):
    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            if url.endswith("/authenticate"):
                return _Resp(200, {"success": True, "result": {"access_token": "jwt-user"}})
            return _Resp(status, {"success": False})

    client = GadsHubClient("http://hub", admin_username="a", admin_password="b", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(
        client, workspace_id="ws", public_url="https://remote.example", clock=lambda: 1_000_000.0
    )
    with pytest.raises(RemoteAccessPlatformError, match=f"gads_tap_failed status={status}"):
        platform.tap(
            device_id=SLOT1_SERIAL,
            platform_username="rental-user",
            platform_password="secret",
            x=540,
            y=960,
        )


def test_farm_tap_input_uses_assigned_slot_serial_only():
    runner = FakeRunner()
    result = execute_farm_task(
        adb_path="adb",
        slot_map={12: "SERIAL-BAY12", 8: "SERIAL-OTHER"},
        request=FarmTaskRequest(
            job_id="job-tap",
            task_type="setup_session_input",
            farm_slot_id=12,
            payload={"kind": "tap", "x": 540, "y": 960},
        ),
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.ok is True
    assert runner.calls == [("SERIAL-BAY12", ["shell", "input", "tap", "540", "960"])]


def test_gads_adapter_tap_200_returns_true():
    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            if url.endswith("/authenticate"):
                return _Resp(200, {"success": True, "result": {"access_token": "jwt-user"}})
            assert url.endswith("/tap")
            assert json == {"x": 12, "y": 40}
            return _Resp(200, {"success": True})

    client = GadsHubClient("http://hub", admin_username="a", admin_password="b", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(
        client, workspace_id="ws", public_url="https://remote.example", clock=lambda: 1_000_000.0
    )
    assert (
        platform.tap(
            device_id=SLOT1_SERIAL,
            platform_username="rental-user",
            platform_password="secret",
            x=12,
            y=40,
        )
        is True
    )
