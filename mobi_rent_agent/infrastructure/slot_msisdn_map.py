"""Optional slot → SIM2 MSISDN map for farm-side reply resolution."""
from __future__ import annotations

import json
from pathlib import Path

from infrastructure.redact import normalize_msisdn


class SlotMsisdnMapError(RuntimeError):
    pass


def load_slot_msisdn_map(path: str | Path | None) -> dict[int, str]:
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        raise SlotMsisdnMapError(f"slot MSISDN map not found: {file_path}")
    raw = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SlotMsisdnMapError("slot MSISDN map must be a JSON object")
    mapping: dict[int, str] = {}
    for key, value in raw.items():
        slot_id = int(key)
        if isinstance(value, dict):
            msisdn = value.get("sim2_msisdn") or value.get("msisdn") or value.get("number")
        else:
            msisdn = value
        if not msisdn:
            raise SlotMsisdnMapError(f"slot {slot_id} missing msisdn")
        mapping[slot_id] = normalize_msisdn(str(msisdn))
    return mapping
