"""Shared refusal rules for leftover silent-provision code paths.

The daemon must never call these paths. Tools and AdbCompanionProvisioner
use this guard so they cannot bypass HumanActivationProvider or the
SlotIsolationPolicy allowlist.
"""
from __future__ import annotations

from domain.slot_isolation import SlotIsolationPolicy


def refuse_legacy_silent_provision(
    *,
    slot_id: int,
    isolation: SlotIsolationPolicy | None = None,
    real_esim_enabled: bool | None = False,
    can_silent_install: bool | None = False,
    authorized: bool = False,
) -> str | None:
    """Return a refusal reason, or None only if every gate passes.

    ``authorized`` defaults to False: even a privileged companion cannot
    use the leftover silent path until a future task explicitly opts in.
    """
    policy = isolation or SlotIsolationPolicy()
    if not policy.allows(slot_id):
        return (
            f"slot {slot_id} is outside the provisioning allowlist "
            f"{sorted(policy.allowed_slot_ids)}"
        )
    if real_esim_enabled is not True:
        return "REAL_ESIM_ENABLED is false; silent provision is refused"
    if can_silent_install is not True:
        return (
            "companion cannot silently install eSIM "
            "(needs WRITE_EMBEDDED_SUBSCRIPTIONS or carrier privileges)"
        )
    if not authorized:
        return "legacy silent provisioner is quarantined; HumanActivationProvider is required"
    return None


def evaluate_legacy_tool_request(
    slot_id: int,
    *,
    isolation: SlotIsolationPolicy | None = None,
    real_esim_enabled: bool = False,
    can_silent_install: bool = False,
) -> str:
    """Tool entry gate. Always unauthorized; always returns a refusal reason."""
    return (
        refuse_legacy_silent_provision(
            slot_id=slot_id,
            isolation=isolation or SlotIsolationPolicy(),
            real_esim_enabled=real_esim_enabled,
            can_silent_install=can_silent_install,
            authorized=False,
        )
        or "legacy silent provisioner is quarantined; HumanActivationProvider is required"
    )
