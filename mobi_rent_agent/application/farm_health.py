"""Classify per-slot farm health without changing device settings."""
from __future__ import annotations

from collections.abc import Mapping

from application.slot_identity import SlotIdentityError, SlotIdentityGuard
from domain.farm import FarmCheck, FarmCheckStatus, FarmSlotReport, FarmSlotState
from infrastructure.redact import redact_serial


def classify_slot(
    slot_id: int,
    *,
    slot_map: Mapping[int, str],
    adb_states: Mapping[str, str],
    voidfix_map: Mapping[int, str] | None = None,
    proxy_ok: bool | None = None,
    sim_ok: bool | None = None,
    send_blocked: bool = False,
    last_error: str | None = None,
) -> FarmSlotReport:
    checks: list[FarmCheck] = []
    serial = str(slot_map.get(slot_id) or "").strip()
    serial_redacted = redact_serial(serial) if serial else None
    adb_state = adb_states.get(serial) if serial else None
    voidfix_id = None if voidfix_map is None else voidfix_map.get(slot_id)
    state = FarmSlotState.UNKNOWN
    attention = None

    try:
        SlotIdentityGuard(slot_map, adb_states=adb_states).verify(slot_id)
        checks.append(FarmCheck("identity", FarmCheckStatus.PASS, "unique mapped device is online", slot_id))
        state = FarmSlotState.HEALTHY
    except SlotIdentityError as exc:
        checks.append(FarmCheck("identity", FarmCheckStatus.FAIL, str(exc), slot_id))
        state = exc.state
        attention = str(exc)

    if voidfix_map is not None and not voidfix_id:
        checks.append(FarmCheck("voidfix", FarmCheckStatus.FAIL, "missing VoidFix device ID", slot_id))
        if state is FarmSlotState.HEALTHY:
            state = FarmSlotState.VOIDFIX_ERROR
        attention = attention or "missing VoidFix mapping"
    elif voidfix_id:
        checks.append(FarmCheck("voidfix", FarmCheckStatus.PASS, "VoidFix device ID present", slot_id))

    if proxy_ok is False:
        checks.append(FarmCheck("proxy", FarmCheckStatus.FAIL, "proxy unavailable or not unique", slot_id))
        if state is FarmSlotState.HEALTHY:
            state = FarmSlotState.PROXY_ERROR
        attention = attention or "proxy error"
    elif proxy_ok is True:
        checks.append(FarmCheck("proxy", FarmCheckStatus.PASS, "proxy assignment present", slot_id))

    if sim_ok is False:
        checks.append(FarmCheck("sim", FarmCheckStatus.FAIL, "SIM unavailable or wrong slot", slot_id))
        if state is FarmSlotState.HEALTHY:
            state = FarmSlotState.SIM_ERROR
        attention = attention or "SIM error"
    elif sim_ok is True:
        checks.append(FarmCheck("sim", FarmCheckStatus.PASS, "SIM check passed", slot_id))
    else:
        checks.append(FarmCheck("sim", FarmCheckStatus.SKIP, "SIM not probed (read-only discovery)", slot_id))

    if send_blocked:
        checks.append(FarmCheck("sms", FarmCheckStatus.FAIL, last_error or "SMS send is blocked", slot_id))
        if state in {FarmSlotState.HEALTHY, FarmSlotState.UNKNOWN}:
            state = FarmSlotState.SEND_BLOCKED
        attention = attention or (last_error or "SMS send is blocked")

    verdict = FarmCheckStatus.PASS if state is FarmSlotState.HEALTHY else FarmCheckStatus.FAIL
    if state is FarmSlotState.UNKNOWN:
        verdict = FarmCheckStatus.WARN
    return FarmSlotReport(
        slot_id=slot_id,
        state=state,
        verdict=verdict,
        serial_redacted=serial_redacted,
        adb_state=adb_state,
        voidfix_device_id=voidfix_id,
        checks=tuple(checks),
        last_error=last_error or attention,
        attention=attention,
    )


def classify_adb_authorization(adb_state: str | None) -> FarmSlotState:
    if adb_state == "device":
        return FarmSlotState.HEALTHY
    if adb_state in {"unauthorized", "offline", "missing", None}:
        return FarmSlotState.DISCONNECTED
    return FarmSlotState.UNKNOWN
