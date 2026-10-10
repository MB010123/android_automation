"""Customer device-status inventory is read-only owned-slot passthrough, never live ADB."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_task_types import SUPPORTED_TASK_TYPES
from application.in_app_control_policy import FORBIDDEN_PAYLOAD_KEYS
from application.vps_api_contract import (
    customer_eid_fields,
    customer_inventory_identity_fields,
    customer_phone_number_for_device_status,
)
from infrastructure.slot_msisdn_map import load_slot_msisdn_map
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

FARM_MSISDN_FIXTURE = {slot: f"+1555123{slot:04d}" for slot in range(1, 21)}
INTERNAL_LEAK_KEYS = (
    "serial",
    "adb_serial",
    "device_serial",
    "udid",
    "device_id",
    "workspace_id",
    "workspace",
    "farm_slot_id",
    "slot_msisdn_map",
)

STORED_EID_A = "89049032012345678901234567890123"
STORED_EID_B = "89049032012345678901234567890124"
STORED_IMEI2_A = "353456789012345"
STORED_IMEI2_B = "353456789012346"
STORED_CARRIER_A = "T-Mobile"
STORED_CARRIER_B = "US Mobile"
STORED_PHONE_A = "+15555550123"
STORED_PHONE_B = "+15555550124"
FAKE_PLACEHOLDER = "00000000000000000000000000000000"
FAKE_IMEI = "000000000000000"
FAKE_PHONE = "+10000000000"
INVENTORY_KEYS = (
    "imei2",
    "imei2_status",
    "eid",
    "eid_status",
    "carrier",
    "carrier_status",
    "phone_number",
    "phone_number_status",
    "cellular_status",
)
STABLE_DEVICE_STATUS_KEYS = (
    "ok",
    "state",
    "online",
    "adb_online",
    "remote_access_available",
    "remote_access_busy",
    "requires_manual_action",
    "session_active",
    "busy",
    "available",
    "slot_id",
    "coordinate_space",
)
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


def test_customer_inventory_identity_fields_known_unknown_and_cellular():
    unknown = customer_inventory_identity_fields()
    assert unknown == {
        "imei2": None,
        "imei2_status": "unknown",
        "eid": None,
        "eid_status": "unknown",
        "carrier": None,
        "carrier_status": "unknown",
        "phone_number": None,
        "phone_number_status": "unknown",
        "cellular_status": "unknown",
    }
    known = customer_inventory_identity_fields(
        eid=STORED_EID_A,
        imei2=STORED_IMEI2_A,
        carrier=STORED_CARRIER_A,
        phone_number=STORED_PHONE_A,
    )
    assert known["imei2"] == STORED_IMEI2_A
    assert known["imei2_status"] == "known"
    assert known["eid"] == STORED_EID_A
    assert known["eid_status"] == "known"
    assert known["carrier"] == STORED_CARRIER_A
    assert known["carrier_status"] == "known"
    assert known["phone_number"] == STORED_PHONE_A
    assert known["phone_number_status"] == "known"
    assert known["cellular_status"] == "unknown"
    whitespace = customer_inventory_identity_fields(eid="  ", imei2="\t", carrier=" ", phone_number="")
    assert whitespace["imei2"] is None and whitespace["imei2_status"] == "unknown"
    assert whitespace["eid"] is None and whitespace["eid_status"] == "unknown"
    assert whitespace["carrier"] is None and whitespace["carrier_status"] == "unknown"
    assert whitespace["phone_number"] is None and whitespace["phone_number_status"] == "unknown"
    assert whitespace["cellular_status"] == "unknown"
    blob = json.dumps(unknown)
    assert FAKE_PLACEHOLDER not in blob
    assert FAKE_IMEI not in blob


def _clear_radio_inventory(tenant: MemoryTenant, bay: int) -> None:
    row = tenant.slots[bay]
    for key in ("imei2", "carrier_name", "carrier", "phone_number", "eid"):
        row.pop(key, None)


def _write_msisdn_map(path: Path, mapping: dict[int, str]) -> Path:
    path.write_text(json.dumps({str(slot): number for slot, number in mapping.items()}), encoding="utf-8")
    return path


def _mapped_farm_service(tmp_path: Path, tenant: MemoryTenant, mapping: dict[int, str], farm: FakeFarm | None = None):
    allowed = tuple(range(1, 21))
    slot_map = {bay: f"SERIAL-SLOT{bay}-TEST" for bay in allowed}
    platform = FakePlatform(registered=set(slot_map.values()))
    return _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=farm or FakeFarm(),
        allowed=allowed,
        slot_map=slot_map,
        workspace_map={bay: f"ws-{bay}" for bay in allowed},
        gads_slot_ids=allowed,
        prepare_slot_ids=allowed,
        observe_slot_ids=allowed,
        slot_msisdn_map_path=str(_write_msisdn_map(tmp_path / "slot_msisdn_map.json", mapping)),
    )


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
    assert status.body["imei2"] == STORED_IMEI2_A
    assert status.body["imei2_status"] == "known"
    assert status.body["carrier"] == STORED_CARRIER_A
    assert status.body["carrier_status"] == "known"
    assert status.body["phone_number"] is None
    assert status.body["phone_number_status"] == "unknown"
    assert status.body["cellular_status"] == "unknown"
    assert "imei1" not in status.body
    assert SLOT1_SERIAL not in json.dumps(status.body)
    assert _farm_types(farm) == ["device_display_size"]
    assert not any(t in MUTATING_FARM_TYPES for t in _farm_types(farm))
    assert [c for c in platform.calls if c[0] == "grant"] == grants_before
    assert tenant.slots[1]["imei2"] == STORED_IMEI2_A


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
    assert home.http_status == 200
    got = service.get_remote_access(CUSTOMER_A, None, rental)
    assert got.body.get("ui_state") != "phone_ready"
    assert got.body.get("setup_complete") is not True
    diag = service.troubleshoot_for_customer(CUSTOMER_A, rental, {})
    assert diag.http_status == 200
    assert diag.body["mode"] == "diagnostics"
    assert diag.body["ui_state"] != "phone_ready"
    assert diag.body["cellular_status"] == "unknown"
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
        assert body["imei2"] == STORED_IMEI2_A
        assert body["imei2_status"] == "known"
        assert body["carrier"] == STORED_CARRIER_A
        assert body["cellular_status"] == "unknown"
        stolen = _http("GET", url, token=token_b)
        assert stolen[0] == 403
        assert stolen[1]["error"] == "rental_not_owned"
        assert stolen[1].get("eid") != STORED_EID_A
        assert stolen[1].get("imei2") != STORED_IMEI2_A
        status, posted, _ = _http(
            "POST",
            url,
            token=token_a,
            body={
                "eid": FAKE_PLACEHOLDER,
                "imei2": FAKE_IMEI,
                "carrier": "FakeCarrier",
                "phone_number": FAKE_PHONE,
                "slot_id": 2,
            },
        )
        assert status == 200
        assert posted["eid"] == STORED_EID_A
        assert posted["eid"] != FAKE_PLACEHOLDER
        assert posted["imei2"] == STORED_IMEI2_A
        assert posted["imei2"] != FAKE_IMEI
        assert posted["carrier"] == STORED_CARRIER_A
        assert posted["cellular_status"] == "unknown"
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


def test_imei2_carrier_phone_known_from_owned_slot(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(
        tenant,
        bay=1,
        user_id=CUSTOMER_A,
        eid=STORED_EID_A,
        imei2=STORED_IMEI2_A,
        carrier_name=STORED_CARRIER_A,
        phone_number=STORED_PHONE_A,
    )
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["imei2"] == STORED_IMEI2_A
    assert status.body["imei2_status"] == "known"
    assert status.body["eid"] == STORED_EID_A
    assert status.body["eid_status"] == "known"
    assert status.body["carrier"] == STORED_CARRIER_A
    assert status.body["carrier_status"] == "known"
    assert status.body["phone_number"] == STORED_PHONE_A
    assert status.body["phone_number_status"] == "known"
    assert status.body["cellular_status"] == "unknown"
    assert "imei1" not in status.body
    assert "carrier_name" not in status.body
    blob = json.dumps(status.body)
    assert SLOT1_SERIAL not in blob
    assert FAKE_IMEI not in blob
    assert FAKE_PLACEHOLDER not in blob
    assert _farm_types(farm) == ["device_display_size"]


def test_missing_and_whitespace_inventory_is_unknown_not_fabricated(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    _clear_radio_inventory(tenant, 1)
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    for key in ("imei2", "eid", "carrier", "phone_number"):
        assert status.body[key] is None
        assert status.body[f"{key}_status"] == "unknown"
    assert status.body["cellular_status"] == "unknown"
    blob = json.dumps(status.body)
    assert FAKE_PLACEHOLDER not in blob
    assert FAKE_IMEI not in blob
    assert STORED_IMEI2_A not in blob
    assert STORED_EID_A not in blob
    assert STORED_PHONE_A not in blob
    assert "dumpsys" not in blob
    whitespace = _rental(
        tenant,
        bay=1,
        user_id=CUSTOMER_A,
        eid="   ",
        imei2="\t",
        carrier_name=" ",
        phone_number="",
    )
    blank = service.device_status_for_customer(CUSTOMER_A, whitespace)
    assert blank.http_status == 200
    assert blank.body["imei2"] is None and blank.body["imei2_status"] == "unknown"
    assert blank.body["eid"] is None and blank.body["eid_status"] == "unknown"
    assert blank.body["carrier"] is None and blank.body["carrier_status"] == "unknown"
    assert blank.body["phone_number"] is None and blank.body["phone_number_status"] == "unknown"
    assert blank.body["cellular_status"] == "unknown"


def test_inventory_cross_rental_and_unassigned_slot_protection(tmp_path: Path):
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
    rental_a = _rental(
        tenant,
        bay=1,
        user_id=CUSTOMER_A,
        eid=STORED_EID_A,
        imei2=STORED_IMEI2_A,
        carrier_name=STORED_CARRIER_A,
        phone_number=STORED_PHONE_A,
    )
    rental_b = _rental(
        tenant,
        bay=2,
        user_id=CUSTOMER_B,
        eid=STORED_EID_B,
        imei2=STORED_IMEI2_B,
        carrier_name=STORED_CARRIER_B,
        phone_number=STORED_PHONE_B,
    )
    stolen = service.device_status_for_customer(CUSTOMER_B, rental_a)
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    for key in INVENTORY_KEYS:
        assert stolen.body.get(key) not in {
            STORED_EID_A,
            STORED_IMEI2_A,
            STORED_CARRIER_A,
            STORED_PHONE_A,
        }
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
        "imei2": STORED_IMEI2_A,
        "carrier_name": STORED_CARRIER_A,
        "phone_number": STORED_PHONE_A,
    }
    empty, _, _ = _service(tmp_path, tenant=unowned, platform=FakePlatform(), farm=FakeFarm())
    denied = empty.device_status_for_customer(CUSTOMER_A, unowned.slots[1]["id"])
    assert denied.http_status in {403, 404}
    assert denied.body.get("imei2") != STORED_IMEI2_A
    assert denied.body.get("eid") != STORED_EID_A
    assert denied.body.get("carrier") != STORED_CARRIER_A
    assert denied.body.get("phone_number") != STORED_PHONE_A
    owned_a = service.device_status_for_customer(CUSTOMER_A, rental_a)
    owned_b = service.device_status_for_customer(CUSTOMER_B, rental_b)
    assert owned_a.body["imei2"] == STORED_IMEI2_A
    assert owned_b.body["imei2"] == STORED_IMEI2_B
    assert owned_a.body["carrier"] == STORED_CARRIER_A
    assert owned_b.body["carrier"] == STORED_CARRIER_B
    assert owned_a.body["phone_number"] == STORED_PHONE_A
    assert owned_b.body["phone_number"] == STORED_PHONE_B
    assert owned_a.body["imei2"] != owned_b.body["imei2"]


def test_cellular_status_unknown_even_when_adb_and_session_online(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(
        tenant,
        bay=1,
        user_id=CUSTOMER_A,
        eid=STORED_EID_A,
        imei2=STORED_IMEI2_A,
        carrier_name=STORED_CARRIER_A,
        phone_number=STORED_PHONE_A,
    )
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["imei2_status"] == "known"
    assert status.body["carrier_status"] == "known"
    assert status.body["phone_number_status"] == "known"
    assert status.body["adb_online"] is True
    assert status.body["online"] is True
    assert status.body["state"] == "online"
    assert status.body["session_active"] is True
    assert status.body["cellular_status"] == "unknown"
    assert status.body["cellular_status"] != "registered"
    assert status.body["cellular_status"] != "connected"


def test_device_status_keeps_existing_phone_fields(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, eid=STORED_EID_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    for key in STABLE_DEVICE_STATUS_KEYS:
        assert key in status.body
    assert status.body["ok"] is True
    assert status.body["state"] == "online"
    assert status.body["online"] is True
    assert status.body["adb_online"] is True
    assert status.body["session_active"] is True
    assert status.body["remote_access_busy"] is False
    assert status.body["busy"] is False
    assert status.body["available"] is True
    assert status.body["slot_id"] == 1
    assert status.body["coordinate_space"] == "native_device_pixels"
    assert status.body["native_resolution"] == {"width": 1440, "height": 3120}
    assert "native_resolution_unavailable" not in status.body
    assert isinstance(status.body["requires_manual_action"], bool)
    assert "imei1" not in status.body
    assert "serial" not in status.body
    assert "udid" not in status.body
    assert "workspace_id" not in status.body
    assert "device_id" not in status.body


def test_device_status_inventory_path_does_not_probe_adb_or_mutate(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(
        tenant,
        bay=1,
        user_id=CUSTOMER_A,
        eid=STORED_EID_A,
        imei2=STORED_IMEI2_A,
        carrier_name=STORED_CARRIER_A,
        phone_number=STORED_PHONE_A,
    )
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
    assert _farm_types(farm).count("airplane_cycle") == 0
    assert "get_eid" not in SUPPORTED_TASK_TYPES
    assert "get_imei" not in SUPPORTED_TASK_TYPES
    assert "device_eid" not in SUPPORTED_TASK_TYPES
    source = (ROOT / "application" / "remote_access_service.py").read_text(encoding="utf-8")
    eid_fn = source[source.find("def _inventory_identity_for_authorized_rental") : source.find("def _tenant_slot_row")]
    assert "EuiccManager(" not in eid_fn
    assert "adb shell" not in eid_fn
    assert "getprop" not in eid_fn
    assert "dumpsys" not in eid_fn
    assert "load_slot_msisdn_map" in eid_fn
    assert "auth.slot_id" in eid_fn
    assert "HEALTH_MONITOR" not in source
    reconnect = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert reconnect.http_status == 501
    assert reconnect.body["error"] == "action_not_supported"


def test_customer_phone_number_farm_map_wins_else_tenant():
    assert customer_phone_number_for_device_status(farm_msisdn="+15550001111", tenant_phone=STORED_PHONE_A) == "+15550001111"
    assert customer_phone_number_for_device_status(farm_msisdn="  ", tenant_phone=STORED_PHONE_A) == STORED_PHONE_A
    assert customer_phone_number_for_device_status(farm_msisdn=None, tenant_phone=STORED_PHONE_A) == STORED_PHONE_A
    assert customer_phone_number_for_device_status(farm_msisdn=None, tenant_phone="  ") is None
    assert customer_phone_number_for_device_status(farm_msisdn="", tenant_phone=None) is None


def test_device_status_phone_from_farm_map_for_every_mapped_slot(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _mapped_farm_service(tmp_path, tenant, FARM_MSISDN_FIXTURE, farm=farm)
    expected = load_slot_msisdn_map(tmp_path / "slot_msisdn_map.json")
    rentals = {
        bay: _rental(tenant, bay=bay, user_id=CUSTOMER_A if bay % 2 else CUSTOMER_B)
        for bay in FARM_MSISDN_FIXTURE
    }
    for bay, rental in rentals.items():
        owner = CUSTOMER_A if bay % 2 else CUSTOMER_B
        status = service.device_status_for_customer(owner, rental)
        assert status.http_status == 200, status.body
        assert status.body["phone_number"] == expected[bay]
        assert status.body["phone_number_status"] == "known"
        assert status.body["imei2_status"] == "known"
        assert status.body["carrier_status"] == "known"
        assert status.body["cellular_status"] == "unknown"
        assert status.body["adb_online"] is True
        assert "native_resolution" in status.body or "native_resolution_unavailable" in status.body
        blob = json.dumps(status.body)
        assert SLOT1_SERIAL not in blob
        assert f"SERIAL-SLOT{bay}-TEST" not in blob
        assert f"ws-{bay}" not in blob
        for key in INTERNAL_LEAK_KEYS:
            assert key not in status.body
    assert _farm_types(farm).count("device_display_size") == len(FARM_MSISDN_FIXTURE)
    assert not any(t in MUTATING_FARM_TYPES for t in _farm_types(farm))


def test_device_status_unmapped_farm_slot_is_unknown_unless_tenant_phone(tmp_path: Path):
    tenant = MemoryTenant()
    mapped = {bay: number for bay, number in FARM_MSISDN_FIXTURE.items() if bay != 20}
    service, _, _ = _mapped_farm_service(tmp_path, tenant, mapped)
    expected = load_slot_msisdn_map(tmp_path / "slot_msisdn_map.json")
    unmapped = _rental(tenant, bay=20, user_id=CUSTOMER_A)
    _clear_radio_inventory(tenant, 20)
    status = service.device_status_for_customer(CUSTOMER_A, unmapped)
    assert status.http_status == 200
    assert status.body["phone_number"] is None
    assert status.body["phone_number_status"] == "unknown"
    assert expected.get(20) is None
    fallback = _rental(tenant, bay=20, user_id=CUSTOMER_A, phone_number=STORED_PHONE_A)
    known = service.device_status_for_customer(CUSTOMER_A, fallback)
    assert known.body["phone_number"] == STORED_PHONE_A
    assert known.body["phone_number_status"] == "known"
    mapped_rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, phone_number=STORED_PHONE_B)
    farm_wins = service.device_status_for_customer(CUSTOMER_A, mapped_rental)
    assert farm_wins.body["phone_number"] == expected[1]
    assert farm_wins.body["phone_number"] != STORED_PHONE_B
    assert farm_wins.body["phone_number_status"] == "known"


def test_device_status_farm_phone_cannot_select_another_slot(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _mapped_farm_service(tmp_path, tenant, FARM_MSISDN_FIXTURE)
    expected = load_slot_msisdn_map(tmp_path / "slot_msisdn_map.json")
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=2, user_id=CUSTOMER_B)
    stolen = service.device_status_for_customer(CUSTOMER_B, rental_a)
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    assert stolen.body.get("phone_number") != expected[1]
    owned_a = service.device_status_for_customer(CUSTOMER_A, rental_a)
    owned_b = service.device_status_for_customer(CUSTOMER_B, rental_b)
    assert owned_a.body["phone_number"] == expected[1]
    assert owned_b.body["phone_number"] == expected[2]
    assert owned_a.body["phone_number"] != owned_b.body["phone_number"]
    assert "slot_id" in FORBIDDEN_PAYLOAD_KEYS
    assert "serial" in FORBIDDEN_PAYLOAD_KEYS
    assert "adb_serial" in FORBIDDEN_PAYLOAD_KEYS
    assert "workspace_id" in FORBIDDEN_PAYLOAD_KEYS


def test_http_device_status_farm_phone_ignores_forbidden_payload(tmp_path: Path):
    map_path = _write_msisdn_map(tmp_path / "slot_msisdn_map.json", {1: FARM_MSISDN_FIXTURE[1], 2: FARM_MSISDN_FIXTURE[2]})
    expected = load_slot_msisdn_map(map_path)
    server, port, tenant, _platform, _ = _start_http(tmp_path, slot_msisdn_map_path=str(map_path))
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "farm-phone-a@example.com")
        token_b, _user_b = _signup(base, "farm-phone-b@example.com")
        rental = _rental(tenant, bay=1, user_id=user_a)
        url = f"{base}/rentals/{rental}/remote-access/device-status"
        status, body, _ = _http("GET", url, token=token_a)
        assert status == 200, body
        assert body["phone_number"] == expected[1]
        assert body["phone_number_status"] == "known"
        assert body["imei2_status"] == "known"
        assert body["cellular_status"] == "unknown"
        assert SLOT1_SERIAL not in json.dumps(body)
        for key in INTERNAL_LEAK_KEYS:
            assert key not in body
        stolen = _http("GET", url, token=token_b)
        assert stolen[0] == 403
        assert stolen[1]["error"] == "rental_not_owned"
        assert stolen[1].get("phone_number") != expected[1]
        status, posted, _ = _http(
            "POST",
            url,
            token=token_a,
            body={
                "slot_id": 2,
                "farm_slot_id": 2,
                "serial": SLOT2_SERIAL,
                "adb_serial": SLOT2_SERIAL,
                "device_id": SLOT2_SERIAL,
                "workspace_id": "ws-2",
                "phone_number": FAKE_PHONE,
            },
        )
        assert status == 200
        assert posted["phone_number"] == expected[1]
        assert posted["phone_number"] != expected[2]
        assert posted["phone_number"] != FAKE_PHONE
        assert posted["slot_id"] == 1
        assert SLOT2_SERIAL not in json.dumps(posted)
    finally:
        server.shutdown()
