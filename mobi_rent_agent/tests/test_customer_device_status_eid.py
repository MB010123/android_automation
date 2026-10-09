"""Customer device-status EID is read-only inventory, never live ADB."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_task_types import SUPPORTED_TASK_TYPES
from application.vps_api_contract import customer_eid_fields
from tests.fakes_supabase import MemoryTenant
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeFarm,
    FakePlatform,
    SLOT1_SERIAL,
    SLOT2_SERIAL,
    _http,
    _rental,
    _service,
    _start_http,
    _signup,
)

STORED_EID_A = "89049032012345678901234567890123"
STORED_EID_B = "89049032012345678901234567890124"
FAKE_PLACEHOLDER = "00000000000000000000000000000000"
MUTATING_FARM_TYPES = {
    "assign",
    "reboot",
    "airplane_cycle",
    "voidfix_repair",
    "setup_session_voidfix_cycle",
    "setup_session_safe_cleanup",
    "remote_access_place_qr",
    "setup_session_input",
}


def _farm_types(farm: FakeFarm) -> list[str]:
    return [str(t["type"]) for t in farm.tasks]


def test_customer_eid_fields_never_invent_placeholder():
    assert customer_eid_fields(None) == {"eid": None, "eid_status": "unknown"}
    assert customer_eid_fields("   ") == {"eid": None, "eid_status": "unknown"}
    assert customer_eid_fields(STORED_EID_A) == {"eid": STORED_EID_A, "eid_status": "known"}
    assert FAKE_PLACEHOLDER not in json.dumps(customer_eid_fields(None))


def test_assigned_rental_reads_stored_eid_only(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, eid=STORED_EID_A)
    grants_before = [c for c in platform.calls if c[0] == "grant"]
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["eid"] == STORED_EID_A
    assert status.body["eid_status"] == "known"
    assert status.body["eid"] != FAKE_PLACEHOLDER
    assert SLOT1_SERIAL not in json.dumps(status.body)
    assert _farm_types(farm) == ["device_display_size"]
    assert not any(t in MUTATING_FARM_TYPES for t in _farm_types(farm))
    assert [c for c in platform.calls if c[0] == "grant"] == grants_before
    assert tenant.slots[1]["imei2"] == "353456789012345"


def test_missing_eid_is_unknown_not_fake(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert "eid" not in tenant.slots[1]
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["eid"] is None
    assert status.body["eid_status"] == "unknown"
    blob = json.dumps(status.body)
    assert FAKE_PLACEHOLDER not in blob
    assert STORED_EID_A not in blob
    assert "dumpsys" not in blob
    assert _farm_types(farm) == ["device_display_size"]


def test_whitespace_eid_is_unknown(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, eid="   ")
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["eid"] is None
    assert status.body["eid_status"] == "unknown"


def test_unassigned_and_cross_rental_cannot_read_eid(tmp_path: Path):
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
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A, eid=STORED_EID_A)
    rental_b = _rental(tenant, bay=2, user_id=CUSTOMER_B, eid=STORED_EID_B)
    stolen = service.device_status_for_customer(CUSTOMER_B, rental_a)
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    assert stolen.body.get("eid") != STORED_EID_A
    missing = service.device_status_for_customer(
        CUSTOMER_A, "00000000-0000-4000-8000-000000000001"
    )
    assert missing.http_status == 404
    unowned = MemoryTenant()
    unowned.slots[1] = {
        "id": "00000000-0000-4000-8000-000000000010",
        "user_id": None,
        "rental_id": None,
        "motherboard_slot_num": 1,
        "eid": STORED_EID_A,
        "imei2": "353456789012345",
    }
    empty, _, _ = _service(tmp_path, tenant=unowned, platform=FakePlatform(), farm=FakeFarm())
    denied = empty.device_status_for_customer(CUSTOMER_A, unowned.slots[1]["id"])
    assert denied.http_status in {403, 404}
    assert denied.body.get("eid") != STORED_EID_A
    owned_a = service.device_status_for_customer(CUSTOMER_A, rental_a)
    owned_b = service.device_status_for_customer(CUSTOMER_B, rental_b)
    assert owned_a.body["eid"] == STORED_EID_A
    assert owned_b.body["eid"] == STORED_EID_B
    assert owned_a.body["eid"] != owned_b.body["eid"]


def test_device_status_does_not_mutate_esim_voidfix_gads_or_farm_identity(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, eid=STORED_EID_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    before = store.get(rental)
    grants_before = [c for c in platform.calls if c[0] == "grant"]
    mutating_before = [t for t in _farm_types(farm) if t in MUTATING_FARM_TYPES]
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    after = store.get(rental)
    assert after.activation_observed == before.activation_observed
    assert after.setup_complete == before.setup_complete
    assert after.prepare_state == before.prepare_state
    assert after.setup_phase == before.setup_phase
    assert [c for c in platform.calls if c[0] == "grant"] == grants_before
    assert [t for t in _farm_types(farm) if t in MUTATING_FARM_TYPES] == mutating_before
    assert "get_eid" not in SUPPORTED_TASK_TYPES
    assert "device_eid" not in SUPPORTED_TASK_TYPES
    assert not any("euicc" in json.dumps(t["payload"]).lower() for t in farm.tasks)
    source = (ROOT / "application" / "remote_access_service.py").read_text(encoding="utf-8")
    assert "EuiccManager(" not in source
    assert "adb shell" not in source
    assert "getprop" not in source
    assert "service call" not in source


def test_imei2_phone_ready_setup_mode_troubleshoot_reconnect_unchanged(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, eid=STORED_EID_A)
    assert tenant.slots[1]["imei2"] == "353456789012345"
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["eid"] == STORED_EID_A
    assert status.body["state"] == "online"
    assert status.body["session_active"] is True
    assert tenant.slots[1]["imei2"] == "353456789012345"
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert home.http_status == 403
    got = service.get_remote_access(CUSTOMER_A, None, rental)
    assert got.body.get("ui_state") != "phone_ready"
    assert got.body.get("setup_complete") is not True
    diag = service.troubleshoot_for_customer(CUSTOMER_A, rental, {})
    assert diag.http_status == 200
    assert diag.body["mode"] == "diagnostics"
    assert diag.body["ui_state"] != "phone_ready"
    assert "reboot" in diag.body["supported_recovery"]
    assert "voidfix_repair" in diag.body["unsupported"]
    assert "esim_delete" in diag.body["unsupported"]
    reconnect = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert reconnect.http_status == 501
    assert reconnect.body["error"] == "action_not_supported"
    session = store.get(rental)
    store.upsert(
        replace(
            session,
            setup_phase="complete",
            setup_complete=True,
            activation_observed="confirmed",
        )
    )
    ready = service.get_remote_access(CUSTOMER_A, None, rental)
    assert ready.body["ui_state"] == "phone_ready"
    home_ready = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert home_ready.http_status == 200
    assert not any(t["type"] == "assign" for t in farm.tasks)
    assert not any(t["type"] == "voidfix_repair" for t in farm.tasks)
    assert not any(t["type"] == "setup_session_voidfix_cycle" for t in farm.tasks)


def test_http_device_status_eid_ownership(tmp_path: Path):
    server, port, tenant, _platform, _ = _start_http(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "eid-a@example.com")
        token_b, _user_b = _signup(base, "eid-b@example.com")
        rental = _rental(tenant, bay=1, user_id=user_a, eid=STORED_EID_A)
        url = f"{base}/rentals/{rental}/remote-access/device-status"
        status, body, _ = _http("GET", url, token=token_a)
        assert status == 200, body
        assert body["eid"] == STORED_EID_A
        assert body["eid_status"] == "known"
        stolen = _http("GET", url, token=token_b)
        assert stolen[0] == 403
        assert stolen[1]["error"] == "rental_not_owned"
        assert stolen[1].get("eid") != STORED_EID_A
        status, posted, _ = _http("POST", url, token=token_a, body={"eid": FAKE_PLACEHOLDER, "slot_id": 2})
        assert status == 200
        assert posted["eid"] == STORED_EID_A
        assert posted["eid"] != FAKE_PLACEHOLDER
        missing_rental = _rental(tenant, bay=1, user_id=user_a)
        tenant.slots[1].pop("eid", None)
        tenant.slots[1]["id"] = missing_rental
        tenant.slots[1]["rental_id"] = missing_rental
        unknown_url = f"{base}/rentals/{missing_rental}/remote-access/device-status"
        status, unknown, _ = _http("GET", unknown_url, token=token_a)
        assert status == 200, unknown
        assert unknown["eid"] is None
        assert unknown["eid_status"] == "unknown"
        assert FAKE_PLACEHOLDER not in json.dumps(unknown)
    finally:
        server.shutdown()
