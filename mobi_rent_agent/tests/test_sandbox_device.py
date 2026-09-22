from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.sandbox_device import (
    PIXEL_7A_1_SERIAL,
    public_sandbox_report,
    require_pixel7a_1,
    sandbox_live_download_armed,
)


def test_require_pixel7a_1_accepts_only_sandbox_serial():
    assert require_pixel7a_1(None) == PIXEL_7A_1_SERIAL
    assert require_pixel7a_1(PIXEL_7A_1_SERIAL) == PIXEL_7A_1_SERIAL
    with pytest.raises(ValueError, match="3C071JEHN14705"):
        require_pixel7a_1("1C101FDF6009EZ")


def test_sandbox_arming_ignores_slot_maps():
    assert sandbox_live_download_armed(real_esim_enabled=True, esim_live_download_armed=True) is True
    assert sandbox_live_download_armed(real_esim_enabled=True, esim_live_download_armed=False) is False
    source = Path(__import__("infrastructure.sandbox_device", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert "load_slot_map" not in source
    assert "range(2" not in source


def test_public_sandbox_report_drops_farm_keys():
    cleaned = public_sandbox_report(
        {
            "serial": "wrong",
            "slot_id": 1,
            "slots": list(range(1, 21)),
            "companion": {"slot_id": 1, "success": True, "device_code": 0},
            "tello_state": {"present": True, "state": 1, "sim_slot_index": 0},
        }
    )
    assert cleaned["serial"] == PIXEL_7A_1_SERIAL
    assert "slot_id" not in cleaned
    assert "slots" not in cleaned
    assert "slot_id" not in cleaned["companion"]
    assert cleaned["companion"]["success"] is True
    assert cleaned["tello_state"]["sim_slot_index"] == 0
