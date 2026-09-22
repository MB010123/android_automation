"""Register a slot IMEI2 (eSIM/digital IMEI) in the device registry.

Does not modify slot_map.json. Does not log raw IMEIs. Does not enable
provisioning, SOCKS5, VoidFix, or auto-recovery.

Reads identifiers from the environment so they are not passed on argv:

    SLOT_IMEI2=... SLOT_IMEI1=... python tools/register_slot_imei.py --slot 1
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import SlotDeviceRecord
from infrastructure.adb_slot_status import load_slot_map
from infrastructure.device_registry import DeviceRegistryError, imei2_for_slot, upsert_slot_record
from infrastructure.imei import is_valid_imei, redact_imei


def main() -> int:
    parser = argparse.ArgumentParser(description="Register slot IMEI2 in device_registry.json")
    parser.add_argument("--slot", type=int, required=True)
    parser.add_argument("--registry", default="device_registry.json")
    parser.add_argument("--slot-map", default="slot_map.json")
    parser.add_argument(
        "--allow-slots-2-20",
        action="store_true",
        help="required to write any slot other than 1",
    )
    args = parser.parse_args()

    if args.slot != 1 and not args.allow_slots_2_20:
        print("refusing to modify slots 2-20; pass --allow-slots-2-20 if intended", file=sys.stderr)
        return 2

    imei2 = os.environ.get("SLOT_IMEI2", "").strip()
    imei1 = os.environ.get("SLOT_IMEI1", "").strip() or None
    if not is_valid_imei(imei2):
        print("SLOT_IMEI2 is missing or not a valid 15-digit IMEI", file=sys.stderr)
        return 2
    if imei1 is not None and not is_valid_imei(imei1):
        print("SLOT_IMEI1 is not a valid 15-digit IMEI", file=sys.stderr)
        return 2

    slot_map = load_slot_map(args.slot_map)
    adb_serial = slot_map.get(args.slot)
    record = SlotDeviceRecord(slot_id=args.slot, imei2=imei2, imei1=imei1)
    try:
        upsert_slot_record(record, path=args.registry, adb_serial=adb_serial)
    except DeviceRegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    stored = imei2_for_slot(args.slot, path=args.registry)
    print(
        json_report(
            args.slot,
            adb_serial,
            stored == imei2,
            redact_imei(imei2),
            redact_imei(imei1) if imei1 else None,
        )
    )
    return 0


def json_report(slot: int, adb_serial: str | None, stored_ok: bool, imei2_redacted: str, imei1_redacted: str | None) -> str:
    import json

    return json.dumps(
        {
            "slot_id": slot,
            "adb_serial": adb_serial,
            "imei2_stored": stored_ok,
            "imei2_redacted": imei2_redacted,
            "imei1_redacted": imei1_redacted,
            "registry": "device_registry.json",
            "lookup": "imei2_for_slot(slot_id)",
        },
        indent=2,
    )


if __name__ == "__main__":
    sys.exit(main())
