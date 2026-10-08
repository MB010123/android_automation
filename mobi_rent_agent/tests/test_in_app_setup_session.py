"""In-app GADS-proxied setup session: isolation, stream, control, complete."""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
from infrastructure.remote_access_store import STATUS_ACTIVE


def test_customer_opens_assigned_phone_only(tmp_path: Path):
    tenant = _tenant()
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    farm = FakeFarm()
    service, store, _ = _service(
        tmp_path, tenant=tenant, platform=platform, farm=farm, allowed=(1, 2)
    )
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=2, user_id=CUSTOMER_B)
    created = service.create_remote_access(CUSTOMER_A, None, rental_a)
    assert created.http_status == 201
    assert created.body["session_mode"] == "in_app"
    assert "platform_login" not in created.body
    assert SLOT1_SERIAL not in json.dumps(created.body)
    secret = store.get(rental_a).platform_secret
    assert secret and "password" not in json.dumps(created.body)

    assert service.control_session(CUSTOMER_B, rental_a, {"action": "tap", "x": 10, "y": 10}).http_status == 403
    assert service.open_stream(CUSTOMER_B, rental_a).http_status == 403
    assert service.create_remote_access(CUSTOMER_A, 2, rental_a).http_status == 403
    assert service.control_session(CUSTOMER_A, rental_b, {"action": "tap", "x": 10, "y": 10}).http_status == 403


def test_wrong_expired_cancelled_and_missing_mapping_denied(tmp_path: Path):
    tenant = _tenant()
    platform = FakePlatform()
    service, store, clock = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    clock.now += 24 * 3600
    assert service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 1, "y": 1}).http_status == 403
    service.release_device(1, rental)
    rental2 = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental2).http_status == 201
    service.revoke_remote_access(CUSTOMER_A, None, rental2)
    assert service.open_stream(CUSTOMER_A, rental2).http_status == 403
    missing = service.create_remote_access(CUSTOMER_A, None, str(uuid.uuid4()))
    assert missing.http_status == 404
    assert missing.body["error"] == "rental_not_found"


def test_missing_workspace_or_device_denied(tmp_path: Path):
    tenant = _tenant()
    rental = _rental(tenant, bay=2, user_id=CUSTOMER_A)
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(registered={SLOT2_SERIAL}),
        farm=FakeFarm(),
        allowed=(2,),
        slot_map={2: SLOT2_SERIAL},
        workspace_map={},
        gads_slot_ids=(2,),
    )
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 503


def test_stream_is_assigned_device_only_and_closes_after_revoke(tmp_path: Path):
    tenant = _tenant()
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=FakeFarm(),
        allowed=(1, 2),
        workspace_map={1: "ws-a", 2: "ws-b"},
    )
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=2, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201
    assert service.create_remote_access(CUSTOMER_B, None, rental_b).http_status == 201
    stream = service.open_stream(CUSTOMER_A, rental_a)
    assert getattr(stream, "device_id") == SLOT1_SERIAL
    other = service.open_stream(CUSTOMER_B, rental_a)
    assert getattr(other, "http_status", None) == 403
    service.revoke_remote_access(CUSTOMER_A, None, rental_a)
    closed = service.open_stream(CUSTOMER_A, rental_a)
    assert getattr(closed, "http_status", None) == 403


def test_stream_fails_closed_if_mapping_changes(tmp_path: Path):
    tenant = _tenant()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    session = store.get(rental)
    store.upsert(replace(session, device_id="OTHER-SERIAL"))
    denied = service.open_stream(CUSTOMER_A, rental)
    assert getattr(denied, "http_status", None) == 503
    assert denied.body["error"] == "phone_unavailable"
    assert service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 1, "y": 1}).http_status == 503


def test_allowed_tap_swipe_and_blocked_controls(tmp_path: Path):
    tenant = _tenant()
    platform = FakePlatform()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 120, "y": 400})
    assert tap.http_status == 200, tap.body
    swipe = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 100, "y": 900, "x2": 100, "y2": 500}
    )
    assert swipe.http_status == 200, swipe.body
    assert service.control_session(CUSTOMER_A, rental, {"action": "home"}).http_status == 200
    assert service.control_session(CUSTOMER_A, rental, {"action": "recents"}).http_status == 200
    shade = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 10, "y": 5, "x2": 10, "y2": 400}
    )
    assert shade.http_status == 403
    assert service.control_session(CUSTOMER_A, rental, {"action": "keyevent"}).http_status == 403
    adb = service.control_session(
        CUSTOMER_A, rental, {"action": "tap", "x": 1, "y": 1, "adb_command": "reboot"}
    )
    assert adb.http_status == 403
    other = service.control_session(
        CUSTOMER_A, rental, {"action": "tap", "x": 1, "y": 1, "udid": SLOT2_SERIAL}
    )
    assert other.http_status == 403
    taps = [c for c in platform.calls if c[0] == "tap"]
    assert taps and taps[0][1]["device_id"] == SLOT1_SERIAL


def test_complete_requires_esim_and_voidfix_then_revokes(tmp_path: Path):
    tenant = _tenant()
    platform = FakePlatform()
    farm = FakeFarm()
    service, store, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=farm,
        voidfix_package="com.voidfix.app",
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    premature = service.complete_setup(CUSTOMER_A, rental)
    assert premature.http_status == 200, premature.body
    assert premature.body["setup_complete"] is False
    assert premature.body["remote_session"] == "closed"
    assert premature.body["esim_deleted"] is False
    assert store.get(rental).status != STATUS_ACTIVE
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    store.set_activation_observed(rental, "confirmed")
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200, done.body
    assert done.body["ui_state"] == "phone_ready"
    assert done.body["remote_session"] == "closed"
    assert store.get(rental).status != STATUS_ACTIVE
    assert SLOT1_SERIAL not in platform.leases


def test_complete_without_voidfix_package_does_not_report_ready(tmp_path: Path):
    tenant = _tenant()
    service, store, _ = _service(
        tmp_path, tenant=tenant, platform=FakePlatform(), farm=FakeFarm()
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    store.set_activation_observed(rental, "confirmed")
    result = service.complete_setup(CUSTOMER_A, rental)
    assert result.http_status == 200, result.body
    assert result.body["voidfix_observed"] == "package_unconfigured"
    assert result.body["setup_complete"] is False
    assert result.body.get("ui_state") != "phone_ready"
    assert result.body["remote_session"] == "closed"


def _tenant():
    from tests.fakes_supabase import MemoryTenant

    return MemoryTenant()
