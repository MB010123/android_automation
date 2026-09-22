from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "slot1_live_download.py"
_SPEC = importlib.util.spec_from_file_location("slot1_live_download", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["slot1_live_download"] = _MOD
_SPEC.loader.exec_module(_MOD)


def test_farm_tool_uses_slot_1_serial_only():
    slot_map = {1: "1C101FDF6009EZ", 2: "1C141FDF600GXF"}
    serial, error = _MOD.resolve_live_download_serial(slot_map)
    assert error is None
    assert serial == "1C101FDF6009EZ"


def test_farm_tool_refuses_slot_map_mismatch():
    slot_map = {1: "WRONG", 2: "1C141FDF600GXF"}
    serial, error = _MOD.resolve_live_download_serial(slot_map)
    assert serial == ""
    assert error == "slot 1 serial mismatch; refusing"


def test_farm_tool_refuses_pixel7a_serial_as_slot_1():
    slot_map = {1: "3C071JEHN14705"}
    serial, error = _MOD.resolve_live_download_serial(slot_map)
    assert serial == ""
    assert error == "slot 1 serial mismatch; refusing"


def test_live_download_refuses_unmapped_euicc_slot():
    observation = _MOD._GATE.EuiccSlotObservation(
        lpa_bound=True,
        lpa_last_slot_id=-1,
        lpa_last_profile_list_result=131082,
        lpa_last_card_id=-2,
        embedded_list_empty=True,
        euicc_enabled=True,
        gsm_sim_state="ABSENT",
    )
    ready, reason = _MOD._GATE.evaluate_slot_gate(observation)
    assert ready is False
    assert "negative" in reason
