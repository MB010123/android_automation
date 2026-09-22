"""Standalone Pixel 7a #1 sandbox identity.

This target is not a farm bay. Tools that import this module must not load
the farm bay serial file or iterate multi-device serial lists.
"""
from __future__ import annotations

from typing import Any

PIXEL_7A_1_SERIAL = "3C071JEHN14705"
# Companion EuiccController still requires LIVE_SLOT_ID on the JSON wire.
# That value is not a PhoneFarmBox bay and is never logged by sandbox tools.
COMPANION_PROTOCOL_SLOT = 1

_FARM_KEYS = frozenset(
    {
        "slot_id",
        "slot",
        "slots",
        "slot_ids",
        "allowed_slot_ids",
        "slot_map",
    }
)


def require_pixel7a_1(serial: str | None) -> str:
    """Return the sandbox serial or raise ValueError. No farm bay lookup."""
    requested = (serial or PIXEL_7A_1_SERIAL).strip()
    if requested != PIXEL_7A_1_SERIAL:
        raise ValueError(f"sandbox tools only accept serial {PIXEL_7A_1_SERIAL}")
    return PIXEL_7A_1_SERIAL


def sandbox_live_download_armed(*, real_esim_enabled: bool, esim_live_download_armed: bool) -> bool:
    return real_esim_enabled is True and esim_live_download_armed is True


def public_sandbox_report(report: dict[str, Any]) -> dict[str, Any]:
    """Drop farm-bay keys so tool output is serial 3C071JEHN14705 only."""

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items() if key not in _FARM_KEYS}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    cleaned = scrub(report)
    if not isinstance(cleaned, dict):
        cleaned = {}
    cleaned["serial"] = PIXEL_7A_1_SERIAL
    return cleaned
