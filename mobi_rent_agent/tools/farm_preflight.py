"""Read-only 20-slot farm preflight.

Does not send SMS, change SIMs, reboot, or modify slot_map/.env.

Usage (from mobi_rent_agent/):
    python tools/farm_preflight.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.farm_preflight import format_preflight, run_preflight
from infrastructure.adb_slot_status import list_adb_device_states, load_slot_map
from infrastructure.voidfix_devices import VoidFixDeviceMapError, load_voidfix_device_map

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SLOT_MAP = ROOT / "slot_map.json"
DEFAULT_VOIDFIX = ROOT / "voidfix_devices.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only 20-slot farm preflight")
    parser.add_argument("--slot-map", default=str(DEFAULT_SLOT_MAP))
    parser.add_argument("--voidfix-map", default=str(DEFAULT_VOIDFIX))
    parser.add_argument("--adb-path", default="adb")
    args = parser.parse_args()

    slot_map = load_slot_map(args.slot_map)
    adb_states = list_adb_device_states(args.adb_path)
    voidfix_map = None
    voidfix_path = Path(args.voidfix_map)
    if voidfix_path.exists():
        try:
            voidfix_map = load_voidfix_device_map(voidfix_path)
        except VoidFixDeviceMapError as exc:
            print(f"[FAIL] voidfix_map: {exc}")
            return 2

    report = run_preflight(
        slot_map=slot_map,
        adb_states=adb_states,
        voidfix_map=voidfix_map,
    )
    print(format_preflight(report))
    if report.overall.value == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
