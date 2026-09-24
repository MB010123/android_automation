"""Slot lifecycle state machine and deterministic derivation.

States
------
available              no assignment, ADB reachable, no active job
assigned               assignment claimed, provisioning job finished OK but
                       ADB not (yet) confirmed reachable by a fresh heartbeat
provisioning           assign job pending/running
requires_manual_action assign job ended: human Settings/LPA step required
online                 assigned + provisioning completed + fresh heartbeat ADB reachable
busy                   non-assign job (reboot, ...) pending/running
offline                fresh heartbeat says ADB not reachable
network_error          reserved: Farm reports a network/radio failure (not emitted by
                       the current Farm health endpoint; never inferred from ADB alone)
failed                 last assign job ended terminally (`provisioning_phase` is
                       `failed` or `unsupported`) and the bay was released. The
                       phase stays distinguishable on the job and on the status
                       body; `status=failed` + `provisioning_phase=unsupported`
                       coexist. Sticky until the next assign job on the bay.
unknown                no heartbeat yet or heartbeat stale (Farm unreachable)

Rules are applied top-down in `derive_slot_state`; see ALLOWED_TRANSITIONS
for the transitions that backend events/jobs may cause. Callers cannot set
state directly; it is always derived from stores.
"""
from __future__ import annotations

from typing import Literal

SlotState = Literal[
    "available",
    "assigned",
    "provisioning",
    "requires_manual_action",
    "online",
    "offline",
    "busy",
    "network_error",
    "failed",
    "unknown",
]

# Assign-job phases that end the assignment attempt without success and
# derive the `failed` slot state. `requires_manual_action` is deliberately
# excluded: the bay is released and the human step is surfaced via
# `provisioning_phase`, not via the slot state.
TERMINAL_FAILED_PHASES: frozenset[str] = frozenset({"failed", "unsupported"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "available": frozenset({"assigned", "provisioning", "busy", "offline", "unknown"}),
    "assigned": frozenset({"provisioning", "online", "offline", "busy", "available", "unknown"}),
    "provisioning": frozenset({"online", "assigned", "failed", "requires_manual_action", "unknown", "offline"}),
    "requires_manual_action": frozenset({"provisioning", "online", "available", "offline", "unknown"}),
    "online": frozenset({"offline", "busy", "network_error", "available", "unknown"}),
    "busy": frozenset({"online", "assigned", "available", "offline", "failed", "unknown"}),
    "offline": frozenset({"online", "assigned", "available", "busy", "unknown"}),
    "network_error": frozenset({"online", "offline", "busy", "unknown"}),
    "failed": frozenset({"available", "provisioning", "offline", "unknown"}),
    "unknown": frozenset(
        {
            "available",
            "assigned",
            "provisioning",
            "requires_manual_action",
            "online",
            "offline",
            "busy",
            "network_error",
            "failed",
        }
    ),
}


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def heartbeat_is_fresh(
    *,
    last_checked_at: float | None,
    farm_ok: bool,
    now: float,
    interval_seconds: float,
    stale_multiplier: float = 3.0,
) -> bool:
    if last_checked_at is None or not farm_ok:
        return False
    return (now - last_checked_at) <= interval_seconds * stale_multiplier


def derive_slot_state(
    *,
    is_assigned: bool,
    active_job_type: str | None,
    last_assign_phase: str | None,
    heartbeat_fresh: bool,
    adb_online: bool | None,
) -> SlotState:
    """Deterministic derivation.

    Order: active job > stale heartbeat > offline > assignment outcome.
    """
    if active_job_type == "assign":
        return "provisioning"
    if active_job_type is not None:
        return "busy"
    if not heartbeat_fresh or adb_online is None:
        return "unknown"
    if not adb_online:
        return "offline"
    if is_assigned:
        if last_assign_phase == "completed":
            return "online"
        if last_assign_phase == "requires_manual_action":
            return "requires_manual_action"
        return "assigned"
    if last_assign_phase in TERMINAL_FAILED_PHASES:
        return "failed"
    return "available"
