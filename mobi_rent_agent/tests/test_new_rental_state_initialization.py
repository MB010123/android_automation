"""New rental always starts pre-activation. Leftover phone/session state is not Phone Ready."""
from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.remote_access_service import RemoteAccessService
from domain.remote_access import this_rental_activation_eligible
from infrastructure.remote_access_store import (
    NEW_RENTAL_SETUP_PHASE,
    SCHEMA_VERSION,
    STATUS_INITIALIZED,
    RemoteAccessSessionStore,
)
from tests.fakes_supabase import MemoryTenant
from tests.test_esim_qr_upload import PNG_BYTES
from tests.test_phone_ready_gate import _assert_not_phone_ready
from tests.test_rental_end import OWNER_A, _harness, _seed
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeClock,
    FakeFarm,
    FakePlatform,
    SLOT1_SERIAL,
    _rental,
    _service,
)

SOURCE = (ROOT / "application" / "remote_access_service.py").read_text(encoding="utf-8")
STORE_SOURCE = (ROOT / "infrastructure" / "remote_access_store.py").read_text(encoding="utf-8")
FARM_SOURCE = (ROOT / "application" / "vps_farm_management_service.py").read_text(encoding="utf-8")


def _leftover_confirmed() -> dict:
    return {
        "verdict": "ACTIVATION_CONFIRMED",
        "esim_profile_present": True,
        "esim_enabled": True,
        "network_registered": True,
        "cellular": True,
        "observation_complete": True,
    }


def _assert_preactivation(session_or_body) -> None:
    if hasattr(session_or_body, "setup_phase"):
        assert session_or_body.setup_complete is False
        assert (session_or_body.setup_phase or NEW_RENTAL_SETUP_PHASE) == NEW_RENTAL_SETUP_PHASE
        assert session_or_body.activation_observed != "confirmed"
        return
    _assert_not_phone_ready(session_or_body)
    assert session_or_body.get("setup_phase", NEW_RENTAL_SETUP_PHASE) == NEW_RENTAL_SETUP_PHASE


def test_brand_new_rental_starts_not_ready(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = _leftover_confirmed()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    _assert_preactivation(created.body)
    _assert_preactivation(store.get(rental))
    got = service.get_remote_access(CUSTOMER_A, None, rental)
    _assert_preactivation(got.body)


def test_previous_rental_phone_ready_does_not_leak(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    old = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.upload_esim_qr(CUSTOMER_A, old, PNG_BYTES).http_status == 200
    assert service.create_remote_access(CUSTOMER_A, None, old).http_status == 201
    farm.activation_details = _leftover_confirmed()
    confirmed = service.activation_status_for_customer(CUSTOMER_A, old)
    assert confirmed.body["activation_state"] == "ACTIVE"
    assert confirmed.body["ui_state"] == "phone_ready"
    assert store.get(old).setup_complete is True
    service.release_device(1, old)

    tenant.slots[1]["user_id"] = CUSTOMER_B
    new = _rental(tenant, bay=1, user_id=CUSTOMER_B)
    created = service.create_remote_access(CUSTOMER_B, None, new)
    assert created.http_status == 201
    _assert_preactivation(created.body)
    _assert_preactivation(store.get(new))
    assert store.get(old).activation_observed == "confirmed"
    refreshed = service.get_remote_access(CUSTOMER_B, None, new)
    _assert_preactivation(refreshed.body)


def test_qr_gads_stream_online_number_settings_do_not_activate(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.inspect_activity = "com.android.settings/.network.telephony.MobileNetworkActivity"
    platform = FakePlatform()
    service, store, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=farm,
        slot_msisdn_map_path=None,
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, phone_number="+15551212")
    placed = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert placed.http_status == 200
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    _assert_preactivation(created.body)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT1_SERIAL
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 20, "y": 40})
    assert tap.http_status == 200
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.body["online"] is True
    assert status.body.get("phone_number") == "+15551212"
    assert status.body.get("ui_state") != "phone_ready"
    assert status.body.get("activation_state") != "ACTIVE"
    got = service.get_remote_access(CUSTOMER_A, None, rental)
    _assert_preactivation(got.body)
    assert store.get(rental).qr_uploaded_at is not None
    settings = service.control_session(CUSTOMER_A, rental, {"action": "settings"})
    assert settings.http_status == 200
    assert settings.body.get("restricted_destination") == "add_esim"


def test_qr_upload_alone_does_not_activate(tmp_path: Path):
    tenant = MemoryTenant()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    placed = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert placed.http_status == 200
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    _assert_preactivation(created.body)
    assert store.get(rental).activation_observed != "confirmed"
    assert this_rental_activation_eligible(
        prepare_state=store.get(rental).prepare_state,
        qr_uploaded_at=store.get(rental).qr_uploaded_at,
    )


def test_only_this_rental_confirmed_activates(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES).http_status == 200
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    farm.activation_details = _leftover_confirmed()
    observed = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert observed.body["activation_state"] == "ACTIVE"
    assert observed.body["ui_state"] == "phone_ready"
    assert store.get(rental).activation_observed == "confirmed"
    assert store.get(rental).setup_complete is True


def test_delayed_old_rental_event_cannot_activate_new(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(),
        farm=farm,
        allowed=(1,),
    )
    old = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.upload_esim_qr(CUSTOMER_A, old, PNG_BYTES).http_status == 200
    assert service.create_remote_access(CUSTOMER_A, None, old).http_status == 201
    farm.activation_details = {**_leftover_confirmed(), "rental_id": old}
    assert service.activation_status_for_customer(CUSTOMER_A, old).body["activation_state"] == "ACTIVE"
    service.release_device(1, old)

    new = _rental(tenant, bay=1, user_id=CUSTOMER_B)
    tenant.slots[1]["user_id"] = CUSTOMER_B
    assert service.upload_esim_qr(CUSTOMER_B, new, PNG_BYTES).http_status == 200
    assert service.create_remote_access(CUSTOMER_B, None, new).http_status == 201
    farm.activation_details = {**_leftover_confirmed(), "rental_id": old}
    delayed = service.activation_status_for_customer(CUSTOMER_B, new)
    _assert_preactivation(delayed.body)
    assert store.get(new).activation_observed != "confirmed"
    assert store.get(old).activation_observed == "confirmed"


def test_cancelled_then_new_rental_starts_clean(tmp_path: Path):
    svc, _w, _j, _a, _res, tenant, _events, _g = _harness(tmp_path)
    farm = FakeFarm()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    svc.set_remote_access(service)
    old = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    assert svc.reserve_slot(1, {"rental_id": old, "user_id": OWNER_A}).http_status == 200
    assert service.upload_esim_qr(OWNER_A, old, PNG_BYTES).http_status == 200
    assert service.create_remote_access(OWNER_A, None, old).http_status == 201
    farm.activation_details = _leftover_confirmed()
    assert service.activation_status_for_customer(OWNER_A, old).body["ui_state"] == "phone_ready"
    cancelled = svc.cancel_rental_for_customer(OWNER_A, old)
    assert cancelled.http_status == 200
    assert store.get(old).status != "active"

    new = _rental(tenant, bay=1, user_id=CUSTOMER_B)
    created = service.create_remote_access(CUSTOMER_B, None, new)
    assert created.http_status == 201
    _assert_preactivation(created.body)


def test_repeated_assignment_is_idempotent(tmp_path: Path):
    tenant = MemoryTenant()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    first = service.initialize_new_rental_state(rental_id=rental, slot_id=1, customer_id=CUSTOMER_A)
    second = service.initialize_new_rental_state(rental_id=rental, slot_id=1, customer_id=CUSTOMER_A)
    assert first is not None and second is not None
    assert first.rental_id == second.rental_id == rental
    assert store.get(rental).status == STATUS_INITIALIZED
    _assert_preactivation(store.get(rental))
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    reused = service.create_remote_access(CUSTOMER_A, None, rental)
    assert reused.http_status == 200
    assert store.get(rental).rental_id == rental
    _assert_preactivation(reused.body)


def test_backend_restart_does_not_resurrect_phone_ready_for_new_rental(tmp_path: Path):
    tenant = MemoryTenant()
    store_path = tmp_path / "shared-ra.sqlite"
    store = RemoteAccessSessionStore(store_path)
    clock = FakeClock()
    farm = FakeFarm()
    kwargs = dict(
        enabled=True,
        allowed_slot_ids=(1,),
        slot_device_map={1: SLOT1_SERIAL},
        platform=FakePlatform(),
        store=store,
        tenant_store=tenant,
        farm_task_client=farm,
        farm_status_fetcher=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": [1],
            "slot_count": 1,
            "adb_online": 1,
        },
        esim_url_prefixes=("https://example.test/",),
        session_ttl_minutes=60,
        clock=clock,
        sleep=clock.sleep,
        background_runner=lambda fn: fn(),
        workspace_map={1: "ws-poc"},
        gads_slot_ids=(1,),
        prepare_slot_ids=(1,),
        observe_slot_ids=(1,),
    )
    first = RemoteAccessService(**kwargs)
    old = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert first.upload_esim_qr(CUSTOMER_A, old, PNG_BYTES).http_status == 200
    assert first.create_remote_access(CUSTOMER_A, None, old).http_status == 201
    farm.activation_details = _leftover_confirmed()
    assert first.activation_status_for_customer(CUSTOMER_A, old).body["ui_state"] == "phone_ready"
    first.release_device(1, old)

    restarted = RemoteAccessService(**kwargs)
    assert store.get(old).activation_observed == "confirmed"
    tenant.slots[1]["user_id"] = CUSTOMER_B
    new = _rental(tenant, bay=1, user_id=CUSTOMER_B)
    created = restarted.create_remote_access(CUSTOMER_B, None, new)
    assert created.http_status == 201
    _assert_preactivation(created.body)
    _assert_preactivation(store.get(new))


def test_stale_device_status_and_remote_access_cannot_resurrect_ready(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = _leftover_confirmed()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    for _ in range(3):
        status = service.device_status_for_customer(CUSTOMER_A, rental)
        got = service.get_remote_access(CUSTOMER_A, None, rental)
        assert status.body["online"] is True
        _assert_preactivation(got.body)
    assert store.get(rental).activation_observed != "confirmed"


def test_customer_cannot_read_other_rental_device_state(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    denied = service.device_status_for_customer(CUSTOMER_B, rental)
    assert denied.http_status == 403
    denied_ra = service.get_remote_access(CUSTOMER_B, None, rental)
    assert denied_ra.http_status == 403
    denied_act = service.activation_status_for_customer(CUSTOMER_B, rental)
    assert denied_act.http_status == 403


def test_schema_migration_adds_qr_uploaded_at(tmp_path: Path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE remote_access_schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO remote_access_schema_version (version) VALUES (4)")
    conn.execute(
        """
        CREATE TABLE remote_access_sessions (
            rental_id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            slot_id INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            platform_username TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            ended_at REAL,
            prepare_state TEXT,
            prepare_detail TEXT,
            prepare_job_id TEXT,
            activation_observed TEXT,
            activation_observed_at REAL,
            activation_evidence TEXT,
            platform_secret TEXT,
            setup_phase TEXT,
            setup_complete INTEGER,
            voidfix_observed TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    store = RemoteAccessSessionStore(path)
    rental = str(uuid.uuid4())
    session = store.initialize_new_rental(
        rental_id=rental,
        customer_id=CUSTOMER_A,
        slot_id=1,
        device_id=SLOT1_SERIAL,
        now=1.0,
    )
    assert session.qr_uploaded_at is None
    assert session.setup_complete is False
    assert session.setup_phase == NEW_RENTAL_SETUP_PHASE
    version = sqlite3.connect(str(path)).execute("SELECT version FROM remote_access_schema_version").fetchone()[0]
    assert int(version) == SCHEMA_VERSION


def test_no_esim_automation_or_device_owner_in_new_flow():
    blob = SOURCE + STORE_SOURCE + FARM_SOURCE
    for forbidden in (
        "downloadSubscription",
        "switchToSubscription",
        "deleteSubscription",
        "lockTaskPackages",
        "setLockTaskPackages",
        "pm hide",
        "euiccManager.download",
    ):
        assert forbidden.lower() not in blob.lower()
    assert "initialize_new_rental_state" in SOURCE
    assert "this_rental_activation_eligible" in SOURCE
    assert "mark_qr_uploaded" in SOURCE
