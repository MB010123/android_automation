from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.farm_preflight import format_preflight, run_preflight
from application.farm_health import classify_slot
from application.slot_identity import SlotIdentityError, SlotIdentityGuard
from domain.farm import FarmCheckStatus, FarmSlotState, ISOLATED_PROTOTYPE_SERIALS

PRODUCTION_MAP = {slot: f"SERIAL-{slot:02d}-ABCD" for slot in range(1, 21)}
PROTOTYPE_SERIAL = next(iter(ISOLATED_PROTOTYPE_SERIALS))
ONLINE = {serial: "device" for serial in PRODUCTION_MAP.values()}


def test_preflight_pass_for_20_online_slots_without_sms():
    report = run_preflight(slot_map=PRODUCTION_MAP, adb_states=ONLINE, audit=False)
    assert len(report.slots) == 20
    assert all(slot.state is not FarmSlotState.MAPPING_ERROR for slot in report.slots)
    assert all(slot.state is not FarmSlotState.DISCONNECTED for slot in report.slots)
    assert report.overall in {FarmCheckStatus.PASS, FarmCheckStatus.WARN}


def test_preflight_fails_missing_device():
    states = dict(ONLINE)
    states.pop(PRODUCTION_MAP[4])
    report = run_preflight(slot_map=PRODUCTION_MAP, adb_states=states, audit=False)
    slot4 = next(slot for slot in report.slots if slot.slot_id == 4)
    assert slot4.state is FarmSlotState.DISCONNECTED
    assert slot4.verdict is FarmCheckStatus.FAIL
    assert report.overall is FarmCheckStatus.FAIL


def test_preflight_fails_wrong_device_and_duplicate_assignment():
    broken = dict(PRODUCTION_MAP)
    broken[5] = broken[6]
    guard_failed = False
    try:
        SlotIdentityGuard(broken, adb_states=ONLINE)
    except SlotIdentityError:
        guard_failed = True
    assert guard_failed is True


def test_preflight_reports_prototype_isolated():
    states = dict(ONLINE)
    states[PROTOTYPE_SERIAL] = "device"
    report = run_preflight(slot_map=PRODUCTION_MAP, adb_states=states, audit=False)
    assert report.prototype_isolated is True
    assert all(PROTOTYPE_SERIAL not in (slot.serial_redacted or "") for slot in report.slots)


def test_preflight_text_has_pass_or_fail_per_slot():
    report = run_preflight(slot_map=PRODUCTION_MAP, adb_states=ONLINE, audit=False)
    text = format_preflight(report)
    for slot_id in range(1, 21):
        assert f"{slot_id:>4}" in text or str(slot_id) in text
    assert "PASS" in text or "FAIL" in text
    assert "1385" not in text


def test_classify_proxy_and_sim_errors():
    proxy = classify_slot(1, slot_map=PRODUCTION_MAP, adb_states=ONLINE, proxy_ok=False)
    assert proxy.state is FarmSlotState.PROXY_ERROR
    sim = classify_slot(1, slot_map=PRODUCTION_MAP, adb_states=ONLINE, sim_ok=False)
    assert sim.state is FarmSlotState.SIM_ERROR
    void = classify_slot(1, slot_map=PRODUCTION_MAP, adb_states=ONLINE, voidfix_map={})
    assert void.state is FarmSlotState.VOIDFIX_ERROR
