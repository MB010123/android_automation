"""Inventory preparation joins Farm files without inventing or copying Slot 1."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.device_inventory_prepare import (
    CUSTOMER_FORBIDDEN_KEYS,
    CompanionObservation,
    LiveImeiObservation,
    observe_companion_imei_access,
    observe_live_imei,
    prepare_device_inventory,
)
from application.farm_task_types import SUPPORTED_TASK_TYPES
from domain.farm import EXPECTED_PRODUCTION_SLOT_COUNT, EXPECTED_PRODUCTION_SLOT_IDS
from domain.models import SlotDeviceRecord

DOC_IMEI = "490154203237518"
DOC_IMEI_B = "356938035643809"
DOC_IMEI_SLOT2 = "353456789012345"
SLOT1_SERIAL = "SERIAL-SLOT-0001"
SLOT2_SERIAL = "SERIAL-SLOT-0002"


def _slot_map_20() -> dict[int, str]:
    mapping = {slot: f"SERIAL-{slot:02d}-ABCD" for slot in range(1, 21)}
    mapping[1] = SLOT1_SERIAL
    mapping[2] = SLOT2_SERIAL
    return mapping


def test_enumerates_20_slots_from_slot_map():
    inventory = prepare_device_inventory(slot_map=_slot_map_20())
    assert len(inventory.slots) == EXPECTED_PRODUCTION_SLOT_COUNT
    assert [slot.slot_id for slot in inventory.slots] == list(range(1, 21))
    assert all(slot.serial_mapped for slot in inventory.slots)
    report = inventory.to_operator_report()
    assert report["slot_count"] == 20
    assert {row["slot_id"] for row in report["slots"]} == set(EXPECTED_PRODUCTION_SLOT_IDS)


def test_missing_serial_stays_unmapped_and_unknown():
    slot_map = _slot_map_20()
    del slot_map[20]
    inventory = prepare_device_inventory(slot_map=slot_map)
    slot20 = inventory.slots[19]
    assert slot20.slot_id == 20
    assert slot20.serial_mapped is False
    assert slot20.serial is None
    assert slot20.imei2 is None
    assert slot20.eid is None
    assert slot20.carrier is None
    payload = slot20.customer_payload()
    assert payload["imei2"] is None
    assert payload["imei2_status"] == "unknown"
    assert payload["eid_status"] == "unknown"


def test_does_not_copy_slot1_identity_onto_other_slots():
    registry = {
        1: SlotDeviceRecord(slot_id=1, imei2=DOC_IMEI, imei1=DOC_IMEI_B),
    }
    msisdn = {1: "+15551230001", 2: "+15551230002"}
    tenant = {
        1: {"imei2": DOC_IMEI, "carrier_name": "US Mobile", "phone_number": "+15551230001", "eid": "89049032012345678901234567890123"},
    }
    inventory = prepare_device_inventory(
        slot_map=_slot_map_20(),
        device_registry=registry,
        farm_msisdn_map=msisdn,
        tenant_slots=tenant,
    )
    slot1 = inventory.slots[0]
    slot2 = inventory.slots[1]
    assert slot1.imei2 == DOC_IMEI
    assert slot1.imei1 == DOC_IMEI_B
    assert slot1.carrier == "US Mobile"
    assert slot1.eid is not None
    assert slot2.imei1 is None
    assert slot2.imei2 is None
    assert slot2.eid is None
    assert slot2.carrier is None
    assert slot2.tenant_phone is None
    assert slot2.farm_sms_phone == "+15551230002"
    assert slot2.imei2 != slot1.imei2
    payload2 = slot2.customer_payload()
    assert payload2["imei2"] is None
    assert payload2["carrier"] is None
    assert payload2["phone_number"] is None
    assert payload2["eid"] is None
    assert DOC_IMEI not in json.dumps(payload2)
    recommended2 = slot2.recommended_public_slots()
    assert recommended2["imei2"] is None
    assert recommended2["carrier_name"] is None
    assert recommended2["phone_number"] is None
    assert recommended2["eid"] is None


def test_missing_fields_stay_unknown_and_are_not_fabricated():
    inventory = prepare_device_inventory(slot_map=_slot_map_20())
    for slot in inventory.slots:
        payload = slot.customer_payload()
        assert payload["imei2"] is None
        assert payload["imei2_status"] == "unknown"
        assert payload["eid"] is None
        assert payload["eid_status"] == "unknown"
        assert payload["carrier"] is None
        assert payload["carrier_status"] == "unknown"
        assert payload["phone_number"] is None
        assert payload["phone_number_status"] == "unknown"
        assert payload["cellular_status"] == "unknown"
        assert slot.imei1 is None
        assert slot.imei2 is None
        assert slot.eid is None
        assert slot.carrier is None
        assert "000000000000000" not in json.dumps(payload)


def test_customer_payload_never_includes_serials_or_imei1():
    inventory = prepare_device_inventory(
        slot_map=_slot_map_20(),
        device_registry={1: SlotDeviceRecord(slot_id=1, imei2=DOC_IMEI, imei1=DOC_IMEI_B)},
        farm_msisdn_map={1: "15551234567"},
    )
    for slot in inventory.slots:
        payload = slot.customer_payload()
        blob = json.dumps(payload)
        assert SLOT1_SERIAL not in blob
        assert SLOT2_SERIAL not in blob
        assert "SERIAL-" not in blob
        assert "imei1" not in payload
        assert CUSTOMER_FORBIDDEN_KEYS.isdisjoint(payload)
        row = slot.operator_row()
        assert "serial" not in row
        assert row.get("serial_last4") != slot.serial
        if slot.serial_mapped:
            assert row["serial_last4"] == slot.serial[-4:]
            assert slot.serial not in json.dumps(row)


def test_farm_sms_phone_is_not_customer_inventory():
    inventory = prepare_device_inventory(
        slot_map=_slot_map_20(),
        farm_msisdn_map={n: f"+1555000{n:04d}" for n in range(1, 21)},
    )
    slot1 = inventory.slots[0]
    assert slot1.farm_sms_phone is not None
    assert slot1.tenant_phone is None
    payload = slot1.customer_payload()
    assert payload["phone_number"] is None
    assert payload["phone_number_status"] == "unknown"
    assert slot1.recommended_public_slots()["phone_number"] is None
    row = slot1.operator_row()
    assert row["phone_number_farm_sms"]["status"] == "known"
    assert row["phone_number_farm_sms"]["source"] == "farm_slot_msisdn_map"
    assert row["phone_number_customer"]["status"] == "unknown"
    assert row["phone_number_farm_sms"]["value_redacted"].startswith("+***")


def test_tenant_slots_are_customer_source_of_truth():
    tenant = {
        2: {
            "imei2": DOC_IMEI_SLOT2,
            "carrier_name": "T-Mobile",
            "phone_number": "+16515550102",
            "eid": "89049032012345678901234567890124",
        }
    }
    inventory = prepare_device_inventory(
        slot_map=_slot_map_20(),
        device_registry={1: SlotDeviceRecord(slot_id=1, imei2=DOC_IMEI)},
        tenant_slots=tenant,
    )
    slot2 = inventory.slots[1]
    payload = slot2.customer_payload()
    assert payload["imei2"] == DOC_IMEI_SLOT2
    assert payload["imei2_status"] == "known"
    assert payload["carrier"] == "T-Mobile"
    assert payload["phone_number"] == "+16515550102"
    assert payload["eid_status"] == "known"
    assert payload["cellular_status"] == "unknown"
    slot1 = inventory.slots[0]
    assert slot1.customer_payload()["imei2"] is None
    assert slot1.recommended_public_slots()["imei2"] == DOC_IMEI


def test_live_permission_denied_does_not_fabricate_imei():
    denied = LiveImeiObservation(
        imei1=None,
        imei2=None,
        imei1_accessible=False,
        imei2_accessible=False,
        error="slot 0: Permission denied; slot 1: Permission denied",
    )
    inventory = prepare_device_inventory(
        slot_map=_slot_map_20(),
        live_imei={1: denied},
    )
    slot1 = inventory.slots[0]
    assert slot1.imei1 is None
    assert slot1.imei2 is None
    assert slot1.live_imei is not None
    assert slot1.live_imei.result == "permission_denied"
    assert slot1.operator_row()["live_imei_probe"]["result"] == "permission_denied"


def test_companion_success_on_one_slot_does_not_copy():
    inventory = prepare_device_inventory(
        slot_map=_slot_map_20(),
        companion={
            19: CompanionObservation(
                ok=True,
                imei1=DOC_IMEI_B,
                imei2=DOC_IMEI,
                imei1_accessible=True,
                imei2_accessible=True,
            )
        },
    )
    assert inventory.slots[18].imei2 == DOC_IMEI
    assert inventory.slots[18].imei2_source == "companion get_imei_access"
    assert inventory.slots[17].imei2 is None
    assert inventory.slots[19].imei2 is None
    assert inventory.slots[18].customer_payload()["imei2"] is None
    assert inventory.slots[18].recommended_public_slots()["imei2"] == DOC_IMEI


def test_companion_unknown_command_is_recorded():
    missing = CompanionObservation(ok=False, unknown_command=True, error="unknown command: get_imei_access")
    inventory = prepare_device_inventory(slot_map=_slot_map_20(), companion={2: missing})
    slot2 = inventory.slots[1]
    assert slot2.companion is not None
    assert slot2.companion.result == "apk_missing_command"
    assert slot2.imei2 is None


def test_observe_live_imei_uses_existing_reader_result():
    class Result:
        imei1 = None
        imei2 = None
        imei1_accessible = False
        imei2_accessible = False
        source = "cmd phone get-imei"
        error = "slot 0: Permission denied; slot 1: Permission denied"

    class Reader:
        def read(self, serial: str) -> Result:
            assert serial == SLOT1_SERIAL
            return Result()

    obs = observe_live_imei(SLOT1_SERIAL, Reader())
    assert obs.result == "permission_denied"
    assert obs.imei1 is None
    assert obs.imei2 is None


def test_observe_companion_classifies_unknown_command():
    class Client:
        def request(self, serial: str, payload: dict) -> dict:
            assert payload == {"command": "get_imei_access"}
            return {"success": False, "error": "unknown command: get_imei_access"}

    obs = observe_companion_imei_access(SLOT2_SERIAL, Client())
    assert obs.unknown_command is True
    assert obs.result == "apk_missing_command"


def test_operator_report_redacts_imei_and_omits_full_serial():
    inventory = prepare_device_inventory(
        slot_map=_slot_map_20(),
        device_registry={1: SlotDeviceRecord(slot_id=1, imei2=DOC_IMEI, imei1=DOC_IMEI_B)},
        farm_msisdn_map={1: "+19522287088"},
    )
    report = inventory.to_operator_report()
    blob = json.dumps(report)
    assert SLOT1_SERIAL not in blob
    assert DOC_IMEI not in blob
    assert DOC_IMEI_B not in blob
    assert "9522287088" not in blob
    assert report["slots"][0]["imei2"]["value_redacted"] == f"****{DOC_IMEI[-4:]}"
    assert report["slots"][0]["phone_number_farm_sms"]["value_redacted"] == "+***7088"
    assert report["production_write"] is False
    assert "get_imei" not in SUPPORTED_TASK_TYPES
    assert "get_eid" not in SUPPORTED_TASK_TYPES


def test_msisdn_map_loader_accepts_utf8_bom(tmp_path):
    from infrastructure.slot_msisdn_map import load_slot_msisdn_map

    path = tmp_path / "slot_msisdn_map.json"
    path.write_bytes(b'\xef\xbb\xbf{"1": "+15551230001"}\n')
    mapping = load_slot_msisdn_map(path)
    assert mapping[1] == "15551230001"


def test_whitespace_tenant_values_stay_unknown():
    inventory = prepare_device_inventory(
        slot_map=_slot_map_20(),
        tenant_slots={3: {"imei2": "  ", "carrier_name": "\t", "phone_number": "", "eid": " "}},
    )
    slot3 = inventory.slots[2]
    payload = slot3.customer_payload()
    assert payload["imei2"] is None
    assert payload["carrier"] is None
    assert payload["phone_number"] is None
    assert payload["eid"] is None
