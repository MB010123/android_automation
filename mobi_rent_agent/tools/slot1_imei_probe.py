"""Slot 1 IMEI / IMEI2 access probe.

Does not write IMEIs to disk, slot_map.json, or logs. Prints a report to
stdout for the operator. Does not send identifiers to the Lovable backend.

Usage (from mobi_rent_agent/):

    python tools/slot1_imei_probe.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_companion import AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.adb_imei import AdbImeiReader
from infrastructure.adb_slot_status import load_slot_map
from infrastructure.imei import is_valid_imei, redact_imei

SLOT = 1


def _public(payload: dict) -> dict:
    """Keep full IMEI only in the operator stdout payload, never in logs."""
    out = dict(payload)
    for key in ("imei1", "imei2"):
        if out.get(key):
            out[f"{key}_redacted"] = redact_imei(str(out[key]))
            out[f"{key}_valid"] = is_valid_imei(str(out[key]))
    return out


def main() -> int:
    slot_map = load_slot_map("slot_map.json")
    serial = slot_map[SLOT]
    runner = AdbCommandRunner(timeout_seconds=15.0)
    adb_result = AdbImeiReader(runner).read(serial)
    companion: dict = {}
    try:
        companion = AdbForwardedJsonClient(
            runner, "mobi_rent.companion", 12.0
        ).request(serial, {"command": "get_imei_access"})
    except Exception as exc:
        companion = {"success": False, "error": str(exc)}

    identity = AdbForwardedJsonClient(
        runner, "mobi_rent.companion", 12.0
    ).request(serial, {"command": "get_identity"})

    report = {
        "slot": SLOT,
        "serial": serial,
        "identity_has_imei": "imei1" in identity or "imei2" in identity,
        "adb": _public(
            {
                "imei1_accessible": adb_result.imei1_accessible,
                "imei2_accessible": adb_result.imei2_accessible,
                "imei1": adb_result.imei1,
                "imei2": adb_result.imei2,
                "source": adb_result.source,
                "error": adb_result.error,
                "esim_imei_slot": adb_result.esim_imei_slot,
            }
        ),
        "companion": _public(companion),
        "recommendation": (
            "Pixel 6 IMEI2 is modem slot index 1 (eSIM / digital IMEI). "
            "Do not treat IMEI1 as sufficient for US Mobile eSIM ordering."
        ),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
