from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.farm import (
    EXPECTED_PRODUCTION_SLOT_COUNT,
    ISOLATED_PROTOTYPE_SERIALS,
    SmsRetryPolicy,
)
from infrastructure.farm_validation import (
    extra_unmapped_serials,
    validate_proxy_uniqueness,
    validate_recipient_allowlist,
    validate_sim_slot,
    validate_slot_map,
    validate_sms_retry,
    validate_timeouts,
    validate_voidfix_map,
)

PRODUCTION_MAP = {slot: f"SERIAL-{slot:02d}-ABCD" for slot in range(1, 21)}
PROTOTYPE_SERIAL = next(iter(ISOLATED_PROTOTYPE_SERIALS))


def test_all_20_slots_configured_correctly():
    result = validate_slot_map(PRODUCTION_MAP)
    assert result.ok is True
    assert EXPECTED_PRODUCTION_SLOT_COUNT == 20


def test_missing_device_in_slot_map():
    incomplete = dict(PRODUCTION_MAP)
    del incomplete[7]
    result = validate_slot_map(incomplete)
    assert result.ok is False
    assert any("missing" in check.detail for check in result.checks)


def test_duplicate_device_in_slot_map():
    broken = dict(PRODUCTION_MAP)
    broken[8] = broken[7]
    result = validate_slot_map(broken)
    assert result.ok is False
    assert any(check.name == "unique_serials" and check.status.value == "FAIL" for check in result.checks)


def test_wrong_device_assigned_to_slot_detects_prototype_leak():
    broken = dict(PRODUCTION_MAP)
    broken[3] = PROTOTYPE_SERIAL
    result = validate_slot_map(broken)
    assert result.ok is False
    assert any(check.name == "prototype_isolation" for check in result.checks)


def test_missing_voidfix_mapping():
    result = validate_voidfix_map({1: "215"}, required_slots=set(range(1, 21)))
    assert result.ok is False
    assert any("missing VoidFix" in check.detail for check in result.checks)


def test_wrong_sim_slot():
    check = validate_sim_slot(1, 0)
    assert check.status.value == "FAIL"
    check = validate_sim_slot(1, 1)
    assert check.status.value == "PASS"


def test_missing_recipient_allowlist():
    result = validate_recipient_allowlist(())
    assert result.ok is False


def test_duplicate_recipients():
    result = validate_recipient_allowlist(("+15551234567", "15551234567"))
    assert result.ok is False


def test_duplicate_proxy_endpoints():
    result = validate_proxy_uniqueness([("host", 1080, "a"), ("host", 1080, "a")])
    assert result.ok is False


def test_invalid_timeouts_and_retries():
    assert validate_timeouts(request_timeout_seconds=0, heartbeat_interval_seconds=15).ok is False
    with pytest.raises(ValueError):
        SmsRetryPolicy(max_attempts=9)
    assert validate_sms_retry(SmsRetryPolicy()).ok is True


def test_prototype_voidfix_id_rejected_from_farm_map():
    result = validate_voidfix_map({1: "1385"}, required_slots={1})
    assert result.ok is False


def test_extra_unmapped_prototype_is_isolated():
    extra, isolated = extra_unmapped_serials(PRODUCTION_MAP, {"SERIAL-01-ABCD", PROTOTYPE_SERIAL})
    assert isolated is True
    assert any("****" in item for item in extra)
    assert PROTOTYPE_SERIAL not in extra
