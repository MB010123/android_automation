"""Stable public slot UUIDs for Lovable ↔ farm bay (1–20).

Uses UUID v5 with a fixed namespace so IDs are deterministic without a extra
mapping file. Optional SLOT_PUBLIC_ID_MAP_PATH JSON can override {uuid: bay}.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

# Fixed namespace (documented; not a secret).
_SLOT_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def public_id_for_farm_slot(farm_slot_id: int) -> str:
    if not 1 <= farm_slot_id <= 20:
        raise ValueError("farm slot out of range")
    return str(uuid.uuid5(_SLOT_NAMESPACE, f"mobi-rent-farm-slot-{farm_slot_id}"))


def farm_slot_for_public_id(public_id: str, overrides: dict[str, int] | None = None) -> int | None:
    text = str(public_id).strip().lower()
    if overrides and text in {k.lower(): v for k, v in overrides.items()}:
        for key, value in overrides.items():
            if key.lower() == text:
                return int(value)
    for slot in range(1, 21):
        if public_id_for_farm_slot(slot).lower() == text:
            return slot
    return None


def load_slot_public_id_overrides(path: str | Path | None) -> dict[str, int]:
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        return {}
    raw = json.loads(file_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        return {}
    mapping: dict[str, int] = {}
    for key, value in raw.items():
        mapping[str(key).strip()] = int(value)
    return mapping
