"""Bay -> VoidFix dashboard device-ID mapping.

Independent of `slot_map.json` (ADB serials) and `device_registry.json`
(IMEI). Live file is gitignored. Copy `voidfix_devices.example.json`.
"""
from __future__ import annotations

import json
from pathlib import Path


class VoidFixDeviceMapError(RuntimeError):
    """The VoidFix device map file is missing or invalid."""


def load_voidfix_device_map(path: str | Path | None) -> dict[int, str]:
    """Load slot_id -> VoidFix device ID. Missing path yields an empty map."""
    if path is None:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        raise VoidFixDeviceMapError(f"VoidFix device map not found: {file_path}")
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VoidFixDeviceMapError(f"Invalid VoidFix device map {file_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise VoidFixDeviceMapError("VoidFix device map must be a JSON object keyed by slot_id")

    mapping: dict[int, str] = {}
    for key, value in raw.items():
        try:
            slot_id = int(key)
        except (TypeError, ValueError) as exc:
            raise VoidFixDeviceMapError("VoidFix device map keys must be slot ids 1-20") from exc
        if not 1 <= slot_id <= 20:
            raise VoidFixDeviceMapError(f"VoidFix device map slot must be 1-20, got {slot_id}")
        device_id = _as_device_id(slot_id, value)
        if slot_id in mapping:
            raise VoidFixDeviceMapError(f"duplicate VoidFix device map slot {slot_id}")
        mapping[slot_id] = device_id
    _reject_duplicate_device_ids(mapping)
    return mapping


def _reject_duplicate_device_ids(mapping: dict[int, str]) -> None:
    seen: dict[str, int] = {}
    for slot_id, device_id in mapping.items():
        previous = seen.get(device_id)
        if previous is not None:
            raise VoidFixDeviceMapError(
                f"duplicate VoidFix device_id shared by slots {previous} and {slot_id}"
            )
        seen[device_id] = slot_id


def _as_device_id(slot_id: int, value: object) -> str:
    if isinstance(value, dict):
        raw = value.get("device_id", value.get("deviceId", value.get("device")))
    else:
        raw = value
    if raw is None or str(raw).strip() == "":
        raise VoidFixDeviceMapError(f"slot {slot_id} is missing a VoidFix device_id")
    device_id = str(raw).strip()
    if device_id.lower().startswith("1c") and len(device_id) >= 10:
        raise VoidFixDeviceMapError(
            f"slot {slot_id} looks like an ADB serial; VoidFix device IDs come from the dashboard"
        )
    return device_id
