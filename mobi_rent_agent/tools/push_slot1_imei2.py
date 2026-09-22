"""Push Slot 1 IMEI 2 onto the Lovable hardware-queue / slots record.

Reads the already-validated local registry. Does not read the phone.
Does not send IMEI 1. Does not modify slot_map.json. Does not enable
provisioning, SOCKS5, VoidFix, or auto-recovery.

    python tools/push_slot1_imei2.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_slot_status import load_slot_map
from infrastructure.config import load_config
from infrastructure.device_registry import imei2_for_slot, load_device_registry
from infrastructure.imei import redact_imei
from infrastructure.imei_backend import HttpImei2Backend, Imei2BackendError


def main() -> int:
    parser = argparse.ArgumentParser(description="Push Slot 1 IMEI 2 to the backend slots record")
    parser.add_argument("--slot", type=int, default=1)
    parser.add_argument("--registry", default="device_registry.json")
    parser.add_argument("--slot-map", default="slot_map.json")
    parser.add_argument(
        "--allow-slots-2-20",
        action="store_true",
        help="required to write any slot other than 1",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="GET the queue and show the Slot 1 lookup without POSTing",
    )
    args = parser.parse_args()

    if args.slot != 1 and not args.allow_slots_2_20:
        print("refusing to modify slots 2-20; pass --allow-slots-2-20 if intended", file=sys.stderr)
        return 2

    config = load_config()
    registry = load_device_registry(args.registry)
    other_slots = sorted(slot for slot in registry if slot != args.slot)
    if other_slots:
        print(f"local registry also has slots {other_slots}; they will not be posted", file=sys.stderr)

    imei2 = imei2_for_slot(args.slot, path=args.registry)
    if not imei2:
        print(f"no local imei2 registered for slot {args.slot}", file=sys.stderr)
        return 2

    slot_map = load_slot_map(args.slot_map)
    adb_serial = slot_map.get(args.slot)
    client = HttpImei2Backend(
        endpoint=config.queue_endpoint,
        hardware_agent_token=config.hardware_agent_token,
        timeout_seconds=config.request_timeout_seconds,
    )
    try:
        before = client.slot_for_bay(args.slot)
        others_before = {
            row.motherboard_slot_num: row.imei2
            for row in client.fetch_queue()
            if row.motherboard_slot_num not in (None, args.slot)
        }
        if args.dry_run:
            report = {
                "dry_run": True,
                "slot_id": args.slot,
                "backend_record_id": before.record_id if before else None,
                "motherboard_slot_num": before.motherboard_slot_num if before else None,
                "hardware_box_id": before.hardware_box_id if before else None,
                "backend_has_imei2": bool(before and before.imei2),
                "backend_imei2_redacted": before.redacted_imei2() if before and before.imei2 else None,
                "local_imei2_redacted": redact_imei(imei2),
                "lookup": "assigned slot_id -> motherboard_slot_num -> imei2",
                "imei1_sent": False,
                "slots_2_20_posted": False,
            }
            print(json.dumps(report, indent=2))
            return 0

        result = client.register_imei2(
            bay=args.slot,
            imei2=imei2,
            allow_slots_2_20=args.allow_slots_2_20,
            adb_serial=adb_serial,
        )
        others_after = {
            row.motherboard_slot_num: row.imei2
            for row in client.fetch_queue()
            if row.motherboard_slot_num not in (None, args.slot)
        }
        if others_after != others_before:
            raise Imei2BackendError("slots 2-20 backend IMEI values changed; aborting report")
        payload = result.to_dict()
        payload["slots_2_20_unchanged"] = True
        print(json.dumps(payload, indent=2))
        return 0
    except Imei2BackendError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
