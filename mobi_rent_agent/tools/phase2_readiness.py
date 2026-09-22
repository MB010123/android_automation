"""Read-only Phase 2 readiness probe for every mapped slot.

Checks, per slot, everything eSIM provisioning and health monitoring will
depend on — WITHOUT changing any device state:

    - ADB state (`adb get-state`)
    - boot completed (`getprop sys.boot_completed`)
    - eUICC/eSIM hardware feature (`pm list features`)
    - radio registration (`dumpsys telephony.registry`)
    - network reachability (single ping from the device)

Usage (from the mobi_rent_agent directory):

    python tools/phase2_readiness.py [--slot-map slot_map.json] [--adb adb]

Exit code 0 when every slot passes every check, 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner
from infrastructure.adb_health import AdbDeviceHealthController
from infrastructure.adb_slot_status import load_slot_map

EUICC_FEATURE = "feature:android.hardware.telephony.euicc"


def probe_slot(runner: AdbCommandRunner, controller: AdbDeviceHealthController, slot_id: int, serial: str) -> dict:
    result = {
        "slot": slot_id,
        "serial": serial,
        "adb": False,
        "boot": False,
        "euicc": False,
        "radio": False,
        "network": False,
        "error": None,
    }
    health = controller.read_health(slot_id, serial)
    result["adb"] = health.adb_online
    result["boot"] = health.boot_completed
    result["radio"] = health.radio_registered
    result["network"] = health.network_reachable
    result["error"] = health.error

    if health.adb_online and health.boot_completed:
        try:
            features = runner.run(serial, ["shell", "pm", "list", "features"]).stdout
            result["euicc"] = EUICC_FEATURE in features.splitlines()
        except AdbCommandError as exc:
            result["error"] = str(exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Phase 2 readiness probe")
    parser.add_argument("--slot-map", default="slot_map.json")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()

    slot_map = load_slot_map(args.slot_map)
    runner = AdbCommandRunner(adb_path=args.adb, timeout_seconds=args.timeout)
    controller = AdbDeviceHealthController(runner)

    with ThreadPoolExecutor(max_workers=min(args.workers, len(slot_map))) as executor:
        futures = [
            executor.submit(probe_slot, runner, controller, slot_id, serial)
            for slot_id, serial in sorted(slot_map.items())
        ]
        rows = [future.result() for future in futures]

    def mark(value: bool) -> str:
        return "PASS" if value else "FAIL"

    print(f"{'slot':>4}  {'serial':<16} {'adb':<5} {'boot':<5} {'euicc':<6} {'radio':<6} {'network':<8} error")
    all_ok = True
    for row in sorted(rows, key=lambda r: r["slot"]):
        checks_ok = all((row["adb"], row["boot"], row["euicc"], row["radio"], row["network"]))
        all_ok = all_ok and checks_ok
        print(
            f"{row['slot']:>4}  {row['serial']:<16} {mark(row['adb']):<5} {mark(row['boot']):<5} "
            f"{mark(row['euicc']):<6} {mark(row['radio']):<6} {mark(row['network']):<8} {row['error'] or '-'}"
        )

    passed = sum(
        1 for row in rows if all((row["adb"], row["boot"], row["euicc"], row["radio"], row["network"]))
    )
    print(f"\n{passed}/{len(rows)} slots fully ready for Phase 2 (eSIM-capable, registered, online)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
