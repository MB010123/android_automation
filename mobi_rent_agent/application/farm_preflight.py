"""Read-only 20-slot farm preflight. Never sends SMS or changes device settings."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from application.farm_health import classify_slot
from domain.farm import (
    EXPECTED_PRODUCTION_SLOT_IDS,
    FarmCheck,
    FarmCheckStatus,
    FarmPreflightReport,
    FarmSlotState,
)
from infrastructure.adb_slot_status import load_slot_map
from infrastructure.farm_audit import append_farm_audit
from infrastructure.farm_validation import (
    extra_unmapped_serials,
    validate_proxy_uniqueness,
    validate_recipient_allowlist,
    validate_slot_map,
    validate_timeouts,
    validate_voidfix_map,
)
from infrastructure.voidfix_devices import load_voidfix_device_map


def run_preflight(
    *,
    slot_map: Mapping[int, str],
    adb_states: Mapping[str, str],
    voidfix_map: Mapping[int, str] | None = None,
    recipient_allowlist: tuple[str, ...] = (),
    proxy_route_keys: list[tuple] | None = None,
    request_timeout_seconds: float = 10.0,
    heartbeat_interval_seconds: float = 15.0,
    sms_enabled: bool = False,
    live_send_authorized: bool = False,
    audit: bool = True,
) -> FarmPreflightReport:
    global_checks: list[FarmCheck] = []
    global_checks.extend(validate_slot_map(slot_map, require_complete_farm=True).checks)
    if voidfix_map is None:
        global_checks.append(
            FarmCheck("voidfix_map", FarmCheckStatus.WARN, "VoidFix map not loaded; SMS blocked")
        )
        voidfix_map = {}
    else:
        required = set(slot_map) if sms_enabled else None
        global_checks.extend(
            validate_voidfix_map(voidfix_map, required_slots=required).checks
        )
    if recipient_allowlist:
        global_checks.extend(validate_recipient_allowlist(recipient_allowlist).checks)
    else:
        global_checks.append(
            FarmCheck(
                "recipient_allowlist",
                FarmCheckStatus.WARN,
                "no recipient allowlist; live SMS is blocked",
            )
        )
    if proxy_route_keys is not None:
        global_checks.extend(validate_proxy_uniqueness(proxy_route_keys).checks)
    global_checks.extend(
        validate_timeouts(
            request_timeout_seconds=request_timeout_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        ).checks
    )
    if live_send_authorized:
        global_checks.append(
            FarmCheck(
                "live_send",
                FarmCheckStatus.WARN,
                "live-send authorization is set; preflight still does not send SMS",
            )
        )
    else:
        global_checks.append(
            FarmCheck("live_send", FarmCheckStatus.PASS, "live SMS is not authorized")
        )

    extra_redacted, prototype_isolated = extra_unmapped_serials(
        slot_map, {serial for serial, state in adb_states.items() if state == "device"}
    )
    if prototype_isolated:
        global_checks.append(
            FarmCheck(
                "prototype_device",
                FarmCheckStatus.PASS,
                "isolated prototype device is online and not in the farm map",
            )
        )

    send_blocked = (not sms_enabled) or (not voidfix_map) or (not recipient_allowlist)
    send_reason = None
    if not sms_enabled:
        send_reason = "VoidFix SMS is disabled"
    elif not voidfix_map:
        send_reason = "VoidFix device map is missing"
    elif not recipient_allowlist:
        send_reason = "recipient allowlist is empty"

    slots = []
    for slot_id in sorted(EXPECTED_PRODUCTION_SLOT_IDS):
        report = classify_slot(
            slot_id,
            slot_map=slot_map,
            adb_states=adb_states,
            voidfix_map=voidfix_map if voidfix_map else None,
            send_blocked=send_blocked,
            last_error=send_reason,
        )
        if slot_id not in slot_map:
            report = classify_slot(
                slot_id,
                slot_map=slot_map,
                adb_states=adb_states,
                send_blocked=True,
                last_error="slot missing from slot map",
            )
        slots.append(report)

    failing_global = any(check.status is FarmCheckStatus.FAIL for check in global_checks)
    failing_slots = any(slot.verdict is FarmCheckStatus.FAIL for slot in slots)
    # ADB-healthy slots still FAIL when SMS is blocked. Overall farm readiness
    # for heartbeat/discovery can PASS mapping if every slot is uniquely online
    # even while SMS stays blocked.
    mapping_failed = any(
        slot.state in {FarmSlotState.MAPPING_ERROR, FarmSlotState.DISCONNECTED}
        for slot in slots
    ) or failing_global and any(
        check.name in {"slot_count", "unique_serials", "serials_present", "prototype_isolation"}
        and check.status is FarmCheckStatus.FAIL
        for check in global_checks
    )
    if mapping_failed or any(
        check.status is FarmCheckStatus.FAIL and check.name.startswith("slot")
        for check in global_checks
    ):
        overall = FarmCheckStatus.FAIL
    elif failing_slots and send_blocked and not mapping_failed:
        overall = FarmCheckStatus.WARN
    elif failing_slots or failing_global:
        overall = FarmCheckStatus.FAIL
    else:
        overall = FarmCheckStatus.PASS

    report = FarmPreflightReport(
        overall=overall,
        slots=tuple(slots),
        global_checks=tuple(global_checks),
        extra_unmapped_redacted=tuple(extra_redacted),
        prototype_isolated=prototype_isolated,
    )
    if audit:
        append_farm_audit(
            {
                "event": "farm_preflight",
                "overall": overall.value,
                "failing_slot_count": len(report.failing_slots),
                "sms_blocked": send_blocked,
                "prototype_isolated": prototype_isolated,
            }
        )
    return report


def load_production_slot_map(path: str | Path) -> dict[int, str]:
    return load_slot_map(path)


def load_optional_voidfix_map(path: str | Path | None) -> dict[int, str] | None:
    if path is None:
        return None
    file_path = Path(path)
    if not file_path.exists():
        return None
    return load_voidfix_device_map(file_path)


def redacted_slot_rows(report: FarmPreflightReport) -> list[dict[str, str]]:
    rows = []
    for slot in report.slots:
        rows.append(
            {
                "slot": str(slot.slot_id),
                "verdict": slot.verdict.value,
                "state": slot.state.value,
                "serial": slot.serial_redacted or "",
                "adb": slot.adb_state or "missing",
                "voidfix": "yes" if slot.voidfix_device_id else "no",
                "attention": slot.attention or "",
            }
        )
    return rows


def format_preflight(report: FarmPreflightReport) -> str:
    lines = [f"FARM PREFLIGHT {report.overall.value}"]
    for check in report.global_checks:
        lines.append(f"  [{check.status.value}] {check.name}: {check.detail}")
    lines.append("SLOT  VERDICT  STATE            ADB          SERIAL")
    for slot in report.slots:
        lines.append(
            f"{slot.slot_id:>4}  {slot.verdict.value:<7} {slot.state.value:<16} "
            f"{(slot.adb_state or 'missing'):<12} {slot.serial_redacted or ''}"
        )
        if slot.attention:
            lines.append(f"      reason: {slot.attention}")
    if report.extra_unmapped_redacted:
        lines.append("UNMAPPED_ONLINE " + ",".join(report.extra_unmapped_redacted))
    if report.prototype_isolated:
        lines.append("PROTOTYPE isolated extra device present and not assigned to a farm slot")
    return "\n".join(lines)
