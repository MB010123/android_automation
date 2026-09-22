"""Read-only validation of farm slot maps, VoidFix bindings, proxy, and env."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from domain.farm import (
    EXPECTED_PRODUCTION_SLOT_COUNT,
    EXPECTED_PRODUCTION_SLOT_IDS,
    FarmCheck,
    FarmCheckStatus,
    ISOLATED_PROTOTYPE_SERIALS,
    PROTOTYPE_VOIDFIX_DEVICE_ID,
    SmsRetryPolicy,
)
from infrastructure.redact import normalize_msisdn, redact_serial


@dataclass(frozen=True)
class FarmValidationResult:
    checks: tuple[FarmCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.status is not FarmCheckStatus.FAIL for check in self.checks)


def validate_slot_map(
    slot_map: Mapping[int, str],
    *,
    require_complete_farm: bool = True,
) -> FarmValidationResult:
    checks: list[FarmCheck] = []
    ids = set(slot_map)
    if require_complete_farm:
        missing = sorted(EXPECTED_PRODUCTION_SLOT_IDS - ids)
        extra = sorted(ids - EXPECTED_PRODUCTION_SLOT_IDS)
        if missing or extra or len(ids) != EXPECTED_PRODUCTION_SLOT_COUNT:
            checks.append(
                FarmCheck(
                    "slot_count",
                    FarmCheckStatus.FAIL,
                    f"expected exactly {EXPECTED_PRODUCTION_SLOT_COUNT} slots "
                    f"1-20; missing={missing} extra={extra}",
                )
            )
        else:
            checks.append(
                FarmCheck(
                    "slot_count",
                    FarmCheckStatus.PASS,
                    f"{EXPECTED_PRODUCTION_SLOT_COUNT} unique slot numbers",
                )
            )
    else:
        bad = sorted(sid for sid in ids if sid not in EXPECTED_PRODUCTION_SLOT_IDS)
        if bad:
            checks.append(
                FarmCheck("slot_ids", FarmCheckStatus.FAIL, f"slot ids outside 1-20: {bad}")
            )
        else:
            checks.append(FarmCheck("slot_ids", FarmCheckStatus.PASS, "slot ids are in 1-20"))

    empty = [sid for sid, serial in slot_map.items() if not str(serial).strip()]
    if empty:
        checks.append(
            FarmCheck("serials_present", FarmCheckStatus.FAIL, f"empty serials on slots {empty}")
        )
    else:
        checks.append(FarmCheck("serials_present", FarmCheckStatus.PASS, "every slot has a serial"))

    serials = [str(serial).strip() for serial in slot_map.values() if str(serial).strip()]
    counts = Counter(serials)
    duplicates = [serial for serial, count in counts.items() if count > 1]
    if duplicates:
        slots = [
            sid
            for sid, serial in slot_map.items()
            if str(serial).strip() in set(duplicates)
        ]
        checks.append(
            FarmCheck(
                "unique_serials",
                FarmCheckStatus.FAIL,
                f"duplicate device serials on slots {sorted(slots)}",
            )
        )
    else:
        checks.append(FarmCheck("unique_serials", FarmCheckStatus.PASS, "serials are unique"))

    leaked = [
        sid
        for sid, serial in slot_map.items()
        if str(serial).strip() in ISOLATED_PROTOTYPE_SERIALS
    ]
    if leaked:
        checks.append(
            FarmCheck(
                "prototype_isolation",
                FarmCheckStatus.FAIL,
                f"isolated prototype serial assigned to farm slots {leaked}",
            )
        )
    else:
        checks.append(
            FarmCheck(
                "prototype_isolation",
                FarmCheckStatus.PASS,
                "prototype serial is not assigned to a farm slot",
            )
        )
    return FarmValidationResult(tuple(checks))


def validate_voidfix_map(
    device_map: Mapping[int, str],
    *,
    required_slots: set[int] | None = None,
    reject_prototype_id: bool = True,
) -> FarmValidationResult:
    checks: list[FarmCheck] = []
    if required_slots:
        missing = sorted(set(required_slots) - set(device_map))
        extra = sorted(set(device_map) - set(required_slots))
        if missing:
            checks.append(
                FarmCheck(
                    "voidfix_coverage",
                    FarmCheckStatus.FAIL,
                    f"missing VoidFix mapping for slots {missing}",
                )
            )
        elif extra:
            checks.append(
                FarmCheck(
                    "voidfix_coverage",
                    FarmCheckStatus.FAIL,
                    f"extra VoidFix mappings for slots {extra}",
                )
            )
        else:
            checks.append(
                FarmCheck("voidfix_coverage", FarmCheckStatus.PASS, "VoidFix map matches required slots")
            )
    elif not device_map:
        checks.append(
            FarmCheck(
                "voidfix_coverage",
                FarmCheckStatus.WARN,
                "VoidFix device map is empty; SMS is blocked",
            )
        )

    counts = Counter(str(value).strip() for value in device_map.values() if str(value).strip())
    duplicates = [device_id for device_id, count in counts.items() if count > 1]
    if duplicates:
        checks.append(
            FarmCheck("voidfix_unique", FarmCheckStatus.FAIL, "duplicate VoidFix device IDs")
        )
    elif device_map:
        checks.append(FarmCheck("voidfix_unique", FarmCheckStatus.PASS, "VoidFix device IDs are unique"))

    if reject_prototype_id and PROTOTYPE_VOIDFIX_DEVICE_ID in {
        str(value).strip() for value in device_map.values()
    }:
        checks.append(
            FarmCheck(
                "voidfix_prototype_isolation",
                FarmCheckStatus.FAIL,
                "prototype VoidFix device ID is present in the farm map",
            )
        )
    return FarmValidationResult(tuple(checks))


def validate_sim_slot(slot_id: int, sim_slot: int | None) -> FarmCheck:
    if sim_slot is None:
        return FarmCheck(
            "sim_slot",
            FarmCheckStatus.WARN,
            "SIM selector not configured; production send will not attach simSlot",
            slot_id,
        )
    if sim_slot not in {1, 2}:
        return FarmCheck(
            "sim_slot",
            FarmCheckStatus.FAIL,
            f"invalid VoidFix SIM selector {sim_slot}; expected 1 or 2",
            slot_id,
        )
    return FarmCheck("sim_slot", FarmCheckStatus.PASS, f"SIM selector {sim_slot} is valid", slot_id)


def validate_recipient_allowlist(recipients: list[str] | tuple[str, ...]) -> FarmValidationResult:
    checks: list[FarmCheck] = []
    normalized = [normalize_msisdn(item) for item in recipients if str(item).strip()]
    if not normalized:
        checks.append(
            FarmCheck(
                "recipient_allowlist",
                FarmCheckStatus.FAIL,
                "recipient allowlist is empty; live SMS is blocked",
            )
        )
        return FarmValidationResult(tuple(checks))
    if any(len(item) < 10 for item in normalized):
        checks.append(
            FarmCheck("recipient_allowlist", FarmCheckStatus.FAIL, "allowlist entry is too short")
        )
    counts = Counter(normalized)
    if any(count > 1 for count in counts.values()):
        checks.append(
            FarmCheck("recipient_allowlist", FarmCheckStatus.FAIL, "duplicate recipients in allowlist")
        )
    else:
        checks.append(
            FarmCheck(
                "recipient_allowlist",
                FarmCheckStatus.PASS,
                f"{len(normalized)} unique allowlisted recipients",
            )
        )
    return FarmValidationResult(tuple(checks))


def validate_proxy_uniqueness(route_keys: list[tuple]) -> FarmValidationResult:
    if not route_keys:
        return FarmValidationResult(
            (
                FarmCheck(
                    "proxy_routes",
                    FarmCheckStatus.WARN,
                    "no proxy routes configured",
                ),
            )
        )
    if len(set(route_keys)) != len(route_keys):
        return FarmValidationResult(
            (FarmCheck("proxy_routes", FarmCheckStatus.FAIL, "duplicate proxy endpoints"),)
        )
    return FarmValidationResult(
        (FarmCheck("proxy_routes", FarmCheckStatus.PASS, "proxy endpoints are unique"),)
    )


def validate_timeouts(
    *,
    request_timeout_seconds: float,
    heartbeat_interval_seconds: float,
    health_interval_seconds: float = 15.0,
) -> FarmValidationResult:
    checks: list[FarmCheck] = []
    if not 1 <= request_timeout_seconds <= 60:
        checks.append(
            FarmCheck(
                "request_timeout",
                FarmCheckStatus.FAIL,
                f"REQUEST_TIMEOUT_SECONDS={request_timeout_seconds} is outside 1-60",
            )
        )
    else:
        checks.append(FarmCheck("request_timeout", FarmCheckStatus.PASS, "request timeout is in range"))
    if not 5 <= heartbeat_interval_seconds <= 300:
        checks.append(
            FarmCheck(
                "heartbeat_interval",
                FarmCheckStatus.FAIL,
                f"HEARTBEAT_INTERVAL_SECONDS={heartbeat_interval_seconds} is outside 5-300",
            )
        )
    else:
        checks.append(FarmCheck("heartbeat_interval", FarmCheckStatus.PASS, "heartbeat interval is in range"))
    if health_interval_seconds <= 0:
        checks.append(FarmCheck("health_interval", FarmCheckStatus.FAIL, "health interval must be positive"))
    return FarmValidationResult(tuple(checks))


def validate_sms_retry(policy: SmsRetryPolicy) -> FarmValidationResult:
    try:
        SmsRetryPolicy(
            max_attempts=policy.max_attempts,
            base_delay_seconds=policy.base_delay_seconds,
            multiplier=policy.multiplier,
            max_delay_seconds=policy.max_delay_seconds,
        )
    except ValueError as exc:
        return FarmValidationResult(
            (FarmCheck("sms_retry", FarmCheckStatus.FAIL, str(exc)),)
        )
    return FarmValidationResult((FarmCheck("sms_retry", FarmCheckStatus.PASS, "retry policy is bounded"),))


def extra_unmapped_serials(
    slot_map: Mapping[int, str],
    live_serials: set[str],
) -> tuple[list[str], bool]:
    mapped = {str(serial).strip() for serial in slot_map.values()}
    extra = sorted(live_serials - mapped)
    prototype_present = bool(set(extra) & ISOLATED_PROTOTYPE_SERIALS)
    return [redact_serial(serial) for serial in extra], prototype_present
