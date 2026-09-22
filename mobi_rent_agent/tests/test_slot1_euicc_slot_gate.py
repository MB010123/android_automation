from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "slot1_euicc_slot_gate.py"
_SPEC = importlib.util.spec_from_file_location("slot1_euicc_slot_gate", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["slot1_euicc_slot_gate"] = _MOD
_SPEC.loader.exec_module(_MOD)

_ECONTROLLER_UNMAPPED = """
===== EUICC CONNECTOR =====
curState=ConnectedState
mEuiccService=android.service.euicc.IEuiccService$Stub$Proxy@80f76e8
2026-09-09 05:44:45 - thread: EuiccService #1 52, action: GET_EUICC_PROFILE_INFO_LIST, result: 131082, params: {slotId=-1}
2026-09-09 05:49:31 - thread: EuiccService #1 46, action: GET_EUICC_PROFILE_INFO_LIST, result: 131082, params: {slotId=-1}
EuiccManager is enabled: true
"""

_ISUB_UNMAPPED = """
Embedded subscriptions: []
Euicc enabled=true
2026-09-09T05:49:31.653089 - updateEmbeddedSubscriptions: cardId=-2, result=[GetEuiccProfileInfoListResult: result=UNKNOWN(131082), isRemovable=true, mProfiles=null]
"""

_ECONTROLLER_MAPPED = """
===== EUICC CONNECTOR =====
curState=ConnectedState
mEuiccService=android.service.euicc.IEuiccService$Stub$Proxy@abc
2026-09-09 06:00:00 - thread: EuiccService #1 1, action: GET_EUICC_PROFILE_INFO_LIST, result: 0, params: {slotId=1}
EuiccManager is enabled: true
"""

_ISUB_MAPPED = """
Embedded subscriptions: [id=2]
2026-09-09T06:00:00 - updateEmbeddedSubscriptions: cardId=1, result=[GetEuiccProfileInfoListResult: result=0, isRemovable=false, mProfiles=[]]
"""


def test_resolve_slot1_serial_only():
    serial, error = _MOD.resolve_slot1_serial({1: "1C101FDF6009EZ", 2: "1C141FDF600GXF"})
    assert error is None
    assert serial == "1C101FDF6009EZ"


def test_resolve_refuses_pixel7a_as_slot_1():
    serial, error = _MOD.resolve_slot1_serial({1: "3C071JEHN14705"})
    assert serial == ""
    assert error == "slot 1 serial mismatch; refusing"


def test_parse_unmapped_lpa_list_uses_latest_entry():
    slot_id, result = _MOD.parse_lpa_profile_list(_ECONTROLLER_UNMAPPED)
    assert slot_id == -1
    assert result == 131082


def test_evaluate_gate_red_when_slot_id_negative():
    observation = _MOD.observation_from_dumps(_ECONTROLLER_UNMAPPED, _ISUB_UNMAPPED, "ABSENT")
    ready, reason = _MOD.evaluate_slot_gate(observation)
    assert observation.lpa_bound is True
    assert observation.lpa_last_card_id == -2
    assert observation.embedded_list_empty is True
    assert ready is False
    assert "negative" in reason


def test_evaluate_gate_green_when_slot_mapped():
    observation = _MOD.observation_from_dumps(_ECONTROLLER_MAPPED, _ISUB_MAPPED, "LOADED")
    ready, reason = _MOD.evaluate_slot_gate(observation)
    assert observation.lpa_last_slot_id == 1
    assert observation.lpa_last_profile_list_result == 0
    assert observation.lpa_last_card_id == 1
    assert ready is True
    assert reason == "slot mapped"
