from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.legacy_silent_guard import (
    evaluate_legacy_tool_request,
    refuse_legacy_silent_provision,
)


def test_guard_refuses_when_real_esim_disabled():
    reason = refuse_legacy_silent_provision(
        slot_id=1,
        isolation=SlotIsolationPolicy({1}),
        real_esim_enabled=False,
        can_silent_install=True,
        authorized=False,
    )
    assert reason is not None
    assert "REAL_ESIM_ENABLED" in reason


def test_guard_refuses_slots_2_to_20_when_allowlist_is_1():
    policy = SlotIsolationPolicy({1})
    for slot_id in range(2, 21):
        reason = refuse_legacy_silent_provision(
            slot_id=slot_id,
            isolation=policy,
            real_esim_enabled=True,
            can_silent_install=True,
            authorized=True,
        )
        assert reason is not None
        assert "allowlist" in reason


def test_tool_refuses_when_real_esim_false():
    reason = evaluate_legacy_tool_request(1, real_esim_enabled=False, can_silent_install=True)
    assert "REAL_ESIM_ENABLED" in reason


def test_tool_refuses_slot_2_with_allowlist_1():
    reason = evaluate_legacy_tool_request(
        2,
        isolation=SlotIsolationPolicy({1}),
        real_esim_enabled=True,
        can_silent_install=True,
    )
    assert "allowlist" in reason


def test_tool_never_authorizes_send_even_if_flags_true():
    reason = evaluate_legacy_tool_request(1, real_esim_enabled=True, can_silent_install=True)
    assert "quarantined" in reason
    assert "HumanActivationProvider" in reason


def test_production_map_claim_scope_remains_slot_1():
    production_map = {slot: f"SERIAL-{slot}" for slot in range(1, 21)}
    assert SlotIsolationPolicy({1}).claim_ids(production_map) == [1]
