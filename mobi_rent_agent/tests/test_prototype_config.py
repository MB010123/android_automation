from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.prototype import PrototypeMode
from infrastructure.prototype_config import (
    PrototypeConfigError,
    load_prototype_config,
    load_prototype_devices,
)

FARM_SERIAL = "1C101FDF6009EZ"


def _write_devices(path: Path, body: dict) -> Path:
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _base_device(**overrides) -> dict:
    item = {
        "device_type": "pixel_7a",
        "role": "test_pixel_7a",
        "adb_serial": "TESTSERIAL7A",
        "voidfix_device_id": "215",
        "enabled": True,
    }
    item.update(overrides)
    return item


def test_prototype_mode_defaults_to_production_disabled(tmp_path: Path):
    devices = _write_devices(tmp_path / "devices.json", {"prototype-device-1": _base_device()})
    config = load_prototype_config(
        env_file=None,
        devices_file=devices,
        slot_map_path=tmp_path / "missing-slot-map.json",
        environ={"PROTOTYPE_ENVIRONMENT": "prototype"},
    )
    assert config.mode is PrototypeMode.PRODUCTION_DISABLED
    assert config.voidfix_enabled is False
    assert config.real_send_confirmation is False


def test_production_environment_is_refused(tmp_path: Path):
    devices = _write_devices(tmp_path / "devices.json", {"prototype-device-1": _base_device()})
    with pytest.raises(PrototypeConfigError, match="prototype"):
        load_prototype_config(
            env_file=None,
            devices_file=devices,
            slot_map_path=None,
            environ={"PROTOTYPE_ENVIRONMENT": "production"},
        )


def test_missing_api_key_and_disabled_voidfix(tmp_path: Path):
    devices = _write_devices(tmp_path / "devices.json", {"prototype-device-1": _base_device()})
    config = load_prototype_config(
        env_file=None,
        devices_file=devices,
        slot_map_path=None,
        environ={
            "PROTOTYPE_ENVIRONMENT": "prototype",
            "VOIDFIX_ENABLED": "true",
            "VOIDFIX_API_KEY": "",
        },
    )
    assert config.voidfix_enabled is True
    assert config.voidfix_api_key is None


def test_empty_allowlist_when_devices_disabled(tmp_path: Path):
    devices = _write_devices(
        tmp_path / "devices.json",
        {"prototype-device-1": _base_device(enabled=False)},
    )
    config = load_prototype_config(
        env_file=None,
        devices_file=devices,
        slot_map_path=None,
        environ={"PROTOTYPE_ENVIRONMENT": "prototype"},
    )
    assert config.allowlist == ()


def test_missing_device_map_raises(tmp_path: Path):
    with pytest.raises(PrototypeConfigError, match="not found"):
        load_prototype_devices(tmp_path / "missing.json")


def test_duplicate_voidfix_ids_rejected(tmp_path: Path):
    path = _write_devices(
        tmp_path / "devices.json",
        {
            "prototype-device-1": _base_device(voidfix_device_id="215"),
            "prototype-device-2": _base_device(adb_serial="OTHERSERIAL", voidfix_device_id="215"),
        },
    )
    with pytest.raises(PrototypeConfigError, match="duplicate VoidFix"):
        load_prototype_devices(path)


def test_production_serial_in_prototype_map_is_rejected(tmp_path: Path):
    devices = _write_devices(
        tmp_path / "devices.json",
        {"prototype-device-1": _base_device(adb_serial=FARM_SERIAL)},
    )
    slot_map = tmp_path / "slot_map.json"
    slot_map.write_text(json.dumps({"1": FARM_SERIAL}), encoding="utf-8")
    with pytest.raises(PrototypeConfigError, match="production slot_map serial"):
        load_prototype_config(
            env_file=None,
            devices_file=devices,
            slot_map_path=slot_map,
            environ={"PROTOTYPE_ENVIRONMENT": "prototype"},
        )


def test_placeholders_are_not_treated_as_mappings(tmp_path: Path):
    devices = load_prototype_devices(
        _write_devices(
            tmp_path / "devices.json",
            {
                "prototype-device-1": _base_device(
                    adb_serial="REPLACE_WITH_TEST_SERIAL",
                    voidfix_device_id="REPLACE_WITH_VOIDFIX_DEVICE_ID",
                    enabled=False,
                )
            },
        )
    )
    assert devices["prototype-device-1"].adb_serial == ""
    assert devices["prototype-device-1"].voidfix_device_id is None
