"""Phone Ready is gated on Farm ACTIVATION_CONFIRMED only."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_farm_management_service import DEVICE_CLEANUP_EVENT
from tests.fakes_supabase import MemoryTenant
from tests.test_esim_qr_upload import PNG_BYTES
from tests.test_rental_end import OWNER_A, OWNER_B, _available, _harness, _seed
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


def _assert_not_phone_ready(body: dict) -> None:
    assert body.get("ui_state") != "phone_ready", body
    assert body.get("setup_complete") is not True
    assert body.get("activation_state") != "ACTIVE"
    assert body.get("activation_observed") != "confirmed"


def test_qr_uploaded_without_activation_is_not_phone_ready(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    placed = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert placed.http_status == 200 and placed.body["placed"] is True
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    _assert_not_phone_ready(created.body)
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200
    assert done.body["ui_state"] == "session_closed"
    assert done.body["setup_complete"] is False
    assert store.get(rental).activation_observed != "confirmed"


def test_online_phone_and_gads_session_are_not_phone_ready(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["state"] == "online"
    assert status.body["session_active"] is True
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT1_SERIAL
    got = service.get_remote_access(CUSTOMER_A, None, rental)
    assert got.http_status == 200
    _assert_not_phone_ready(got.body)
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert home.http_status == 403


def test_unconfirmed_observer_is_not_phone_ready(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = {"verdict": "ACTIVATION_PARTIAL"}
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    observed = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert observed.http_status == 200
    _assert_not_phone_ready(observed.body)
    assert observed.body["activation_state"] == "ACTIVATING"
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.body["ui_state"] != "phone_ready"
    assert store.get(rental).activation_observed != "confirmed"


def test_activation_confirmed_transitions_to_phone_ready(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    farm = FakeFarm()
    farm.activation_details = {"verdict": "ACTIVATION_CONFIRMED"}
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    observed = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert observed.http_status == 200
    assert observed.body["activation_state"] == "ACTIVE"
    assert observed.body["activation_observed"] == "confirmed"
    assert observed.body["ui_state"] == "phone_ready"
    assert observed.body["setup_complete"] is True
    assert store.get(rental).activation_evidence["verdict"] == "ACTIVATION_CONFIRMED"

    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    recents = service.control_session(CUSTOMER_A, rental, {"action": "recents"})
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 40, "y": 80})
    assert home.http_status == 200 and recents.http_status == 200 and tap.http_status == 200

    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200
    assert done.body["ui_state"] == "phone_ready"
    assert done.body["setup_complete"] is True
    assert done.body["esim_deleted"] is False
    assert done.body["factory_reset"] is False
    assert not any(t["type"] == "setup_session_voidfix_cycle" for t in farm.tasks)
    blob = json.dumps(done.body)
    assert SLOT1_SERIAL not in blob
    assert "EuiccManager" not in blob


def test_complete_reports_phone_ready_after_confirmed_even_if_session_expired(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, clock = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    store.set_activation_observed(rental, "confirmed")
    session = store.get(rental)
    clock.now = float(session.expires_at) + 1
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200
    assert done.body["ui_state"] == "phone_ready"
    assert done.body["setup_complete"] is True
    assert done.body["remote_session"] == "closed"
    assert done.body["esim_deleted"] is False
    assert done.body["factory_reset"] is False


def test_phone_ready_does_not_change_voidfix_or_allow_cross_rental(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = {"verdict": "ACTIVATION_CONFIRMED"}
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(),
        farm=farm,
        voidfix_package="com.voidfix.app",
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    stolen = service.complete_setup(CUSTOMER_B, rental)
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    stolen_ctrl = service.control_session(CUSTOMER_B, rental, {"action": "home"})
    assert stolen_ctrl.http_status == 403
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200
    assert done.body["ui_state"] == "phone_ready"
    assert any(t["type"] == "setup_session_voidfix_cycle" for t in farm.tasks)
    assert done.body["esim_deleted"] is False
    assert done.body["factory_reset"] is False


def test_cancel_from_setup_and_ready_still_requires_cleanup(tmp_path: Path):
    svc, _w, _j, _a, _res, tenant, events, _g = _harness(tmp_path)
    farm = FakeFarm()
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    service, store, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=farm,
        allowed=(1, 2),
        workspace_map={1: "ws-1", 2: "ws-2"},
        gads_slot_ids=(1, 2),
        prepare_slot_ids=(1, 2),
        observe_slot_ids=(1, 2),
    )
    svc.set_remote_access(service)

    setup = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    assert svc.reserve_slot(1, {"rental_id": setup, "user_id": OWNER_A}).http_status == 200
    assert service.create_remote_access(OWNER_A, None, setup).http_status == 201
    cancelled_setup = svc.cancel_rental_for_customer(OWNER_A, setup)
    assert cancelled_setup.http_status == 200
    assert svc._cleanup.is_required(1)
    assert DEVICE_CLEANUP_EVENT in [e.event_type for e in events.list_events(1)]
    assert 1 not in _available(svc)

    tenant.ensure_profile(OWNER_B, "b@example.com")
    ready = _seed(tenant, bay=2, rental_id=str(uuid.uuid4()), user_id=OWNER_B)
    assert svc.reserve_slot(2, {"rental_id": ready, "user_id": OWNER_B}).http_status == 200
    assert service.create_remote_access(OWNER_B, None, ready).http_status == 201
    store.set_activation_observed(ready, "confirmed")
    farm.activation_details = {"verdict": "ACTIVATION_CONFIRMED"}
    done = service.complete_setup(OWNER_B, ready)
    assert done.body["ui_state"] == "phone_ready"
    cancelled_ready = svc.cancel_rental_for_customer(OWNER_B, ready)
    assert cancelled_ready.http_status == 200
    assert svc._cleanup.is_required(2)
    assert DEVICE_CLEANUP_EVENT in [e.event_type for e in events.list_events(2)]
    assert 2 not in _available(svc)
    stranger = svc.cancel_rental_for_customer(OWNER_A, ready)
    assert stranger.http_status in {403, 404}
