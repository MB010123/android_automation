"""Read-only probe of the on-device companion sockets.

Sends get_identity / get_health / get_esim_status / ping over the existing
ADB-forwarded JSON-line transport. Does not enable provisioning, proxy
reconciliation, health reboot, or VoidFix.

Usage (from mobi_rent_agent/):

    python tools/companion_probe.py --slot 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_companion import AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.adb_slot_status import load_slot_map

SOCKETS = ("mobi_rent.companion", "mobi_rent.provisioning", "mobi_rent.network")


def probe(serial: str, adb_path: str, timeout: float) -> dict:
    runner = AdbCommandRunner(adb_path=adb_path, timeout_seconds=timeout)
    results: dict[str, dict] = {}
    commands = {
        "mobi_rent.companion": {"command": "get_identity"},
        "mobi_rent.provisioning": {"command": "get_esim_status"},
        "mobi_rent.network": {"command": "vpn_status"},
    }
    for socket_name, payload in commands.items():
        client = AdbForwardedJsonClient(runner, socket_name, timeout)
        try:
            results[socket_name] = client.request(serial, payload)
        except Exception as exc:
            results[socket_name] = {"success": False, "error": str(exc)}
    health_client = AdbForwardedJsonClient(runner, "mobi_rent.companion", timeout)
    try:
        results["health"] = health_client.request(serial, {"command": "get_health"})
    except Exception as exc:
        results["health"] = {"success": False, "error": str(exc)}
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the on-device companion")
    parser.add_argument("--slot", type=int, default=1)
    parser.add_argument("--slot-map", default="slot_map.json")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--assign", action="store_true", help="send assign_slot for this bay")
    args = parser.parse_args()

    slot_map = load_slot_map(args.slot_map)
    serial = slot_map[args.slot]
    print(f"slot {args.slot} serial {serial}")
    if args.assign:
        runner = AdbCommandRunner(adb_path=args.adb, timeout_seconds=args.timeout)
        client = AdbForwardedJsonClient(runner, "mobi_rent.companion", args.timeout)
        assigned = client.request(serial, {"command": "assign_slot", "slot_id": args.slot})
        print("assign_slot", json.dumps(assigned))
    results = probe(serial, args.adb, args.timeout)
    print(json.dumps(results, indent=2))
    ok = all(isinstance(body, dict) and body.get("success") is not False or "error" not in body for body in results.values())
    # A missing companion is a failed probe.
    failed = any(isinstance(body, dict) and body.get("error") for body in results.values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
