from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import SlotDeviceRecord
from infrastructure.device_registry import (
    DeviceRegistryError,
    imei2_for_slot,
    load_device_registry,
    upsert_slot_record,
)
from infrastructure.imei import is_valid_imei

# Public GSM documentation IMEI, not a farm device.
DOC_IMEI = "490154203237518"
DOC_IMEI_B = "356938035643809"


def test_doc_imeis_are_valid():
    assert is_valid_imei(DOC_IMEI)
    assert is_valid_imei(DOC_IMEI_B)


def test_upsert_and_lookup_by_slot(tmp_path, caplog):
    path = tmp_path / "device_registry.json"
    record = SlotDeviceRecord(slot_id=1, imei2=DOC_IMEI, imei1=DOC_IMEI_B)
    with caplog.at_level(logging.INFO):
        upsert_slot_record(record, path=path, adb_serial="1C101FDF6009EZ")
    assert imei2_for_slot(1, path=path) == DOC_IMEI
    assert imei2_for_slot(2, path=path) is None
    loaded = load_device_registry(path)
    assert loaded[1].imei2 == DOC_IMEI
    assert loaded[1].imei1 == DOC_IMEI_B
    assert "adb_serial" not in json.loads(path.read_text())["1"]
    assert DOC_IMEI not in caplog.text
    assert "4901…7518" in caplog.text
    assert "imei1" not in record.us_mobile_device_info()
    assert record.us_mobile_device_info()["imei2"] == DOC_IMEI


def test_rejects_invalid_luhn(tmp_path):
    path = tmp_path / "device_registry.json"
    record = SlotDeviceRecord(slot_id=1, imei2="490154203237519")
    with pytest.raises(DeviceRegistryError):
        upsert_slot_record(record, path=path)


def test_rejects_imei_equal_to_adb_serial(tmp_path):
    path = tmp_path / "device_registry.json"
    record = SlotDeviceRecord(slot_id=1, imei2=DOC_IMEI)
    with pytest.raises(DeviceRegistryError, match="ADB serial"):
        upsert_slot_record(record, path=path, adb_serial=DOC_IMEI)


def test_rejects_adb_serial_field_in_file(tmp_path):
    path = tmp_path / "device_registry.json"
    path.write_text(json.dumps({"1": {"imei2": DOC_IMEI, "adb_serial": "ABC"}}), encoding="utf-8")
    with pytest.raises(DeviceRegistryError, match="adb_serial"):
        load_device_registry(path)
