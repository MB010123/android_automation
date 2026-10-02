"""Physical farm inventory derived from Farm Agent health.

VPS availability must never use a hardcoded 1–20 bay list. Mapped bays
come from Farm `slot_map` as reported by GET /agent/health. One offline
bay does not make the Farm Agent unreachable.
"""
from __future__ import annotations

from typing import Any

FARM_AGENT_UNAVAILABLE_ERRORS = frozenset(
    {
        "farm_agent_not_configured",
        "farm_auth_failed",
        "adb_unavailable",
        "invalid_farm_response",
        "farm_unreachable",
    }
)


def farm_agent_unavailable(farm: Any) -> bool:
    """True when the Farm Agent cannot report inventory (not when one bay is offline)."""
    if not isinstance(farm, dict):
        return True
    error = str(farm.get("error") or "").strip()
    if error in FARM_AGENT_UNAVAILABLE_ERRORS:
        return True
    if "mapped_slots" in farm:
        return False
    if farm.get("ok") is True:
        return False
    return True


def mapped_farm_slots(farm: dict[str, Any]) -> frozenset[int]:
    """Bays present in Farm slot_map. Empty when Farm omitted mapped_slots (fail closed)."""
    raw = farm.get("mapped_slots")
    if not isinstance(raw, (list, tuple, set)):
        return frozenset()
    bays: set[int] = set()
    for item in raw:
        try:
            bay = int(item)
        except (TypeError, ValueError):
            continue
        if bay >= 1:
            bays.add(bay)
    return frozenset(bays)


def offline_farm_slots(farm: dict[str, Any]) -> frozenset[int]:
    raw = farm.get("offline_slots")
    if not isinstance(raw, (list, tuple, set)):
        return frozenset()
    bays: set[int] = set()
    for item in raw:
        try:
            bay = int(item)
        except (TypeError, ValueError):
            continue
        if bay >= 1:
            bays.add(bay)
    return frozenset(bays)
