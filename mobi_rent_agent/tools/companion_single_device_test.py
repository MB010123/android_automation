"""Single-device companion acceptance test for one Pixel 6.

Does not enable provisioning, production SOCKS5, health reboot, or VoidFix.
Does not modify slot_map.json.

Usage (from mobi_rent_agent/):

    python tools/companion_single_device_test.py --slot 1
    python tools/companion_single_device_test.py --slot 1 --apk ..\\android_companion\\app\\build\\outputs\\apk\\debug\\app-debug.apk
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_companion import AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.adb_slot_status import load_slot_map

PACKAGE = "com.mobirent.companion"
ACTIVITY = "com.mobirent.companion/.MainActivity"


def adb(serial: str, adb_path: str, *args: str) -> str:
    completed = subprocess.run(
        [adb_path, "-s", serial, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "adb failed")
    return completed.stdout.strip()


def request(serial: str, adb_path: str, socket_name: str, payload: dict, timeout: float) -> dict:
    runner = AdbCommandRunner(adb_path=adb_path, timeout_seconds=timeout)
    return AdbForwardedJsonClient(runner, socket_name, timeout).request(serial, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Single-device companion acceptance test")
    parser.add_argument("--slot", type=int, default=1)
    parser.add_argument("--slot-map", default="slot_map.json")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--apk", default="")
    args = parser.parse_args()

    slot_map = load_slot_map(args.slot_map)
    serial = slot_map[args.slot]
    report: dict = {"slot": args.slot, "serial": serial, "checks": {}}

    state = adb(serial, args.adb, "get-state")
    report["checks"]["adb_online"] = state == "device"

    if args.apk:
        apk = Path(args.apk)
        if not apk.is_file():
            print(f"APK not found: {apk}", file=sys.stderr)
            return 1
        adb(serial, args.adb, "install", "-r", str(apk))
        report["checks"]["apk_installed"] = True

    for permission in (
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.READ_PHONE_STATE",
    ):
        subprocess.run(
            [args.adb, "-s", serial, "shell", "pm", "grant", PACKAGE, permission],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    subprocess.run(
        [args.adb, "-s", serial, "shell", "appops", "set", PACKAGE, "ACTIVATE_VPN", "allow"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    adb(
        serial,
        args.adb,
        "shell",
        "am",
        "start",
        "-n",
        ACTIVITY,
        "--ei",
        "slot_id",
        str(args.slot),
    )
    time.sleep(2)

    assigned = request(
        serial,
        args.adb,
        "mobi_rent.companion",
        {"command": "assign_slot", "slot_id": args.slot},
        args.timeout,
    )
    report["assign_slot"] = assigned
    report["checks"]["assign_slot"] = assigned.get("success") is True and assigned.get("slot_id") == args.slot

    identity = request(serial, args.adb, "mobi_rent.companion", {"command": "get_identity"}, args.timeout)
    report["identity"] = identity
    report["checks"]["identity"] = (
        identity.get("success") is True
        and bool(identity.get("device_id"))
        and identity.get("slot_id") == args.slot
        and "Pixel 6" in str(identity.get("model", ""))
    )

    health = request(serial, args.adb, "mobi_rent.companion", {"command": "get_health"}, args.timeout)
    report["health"] = health
    report["checks"]["health"] = health.get("success") is True and health.get("app_healthy") is True

    esim = request(serial, args.adb, "mobi_rent.provisioning", {"command": "get_esim_status"}, args.timeout)
    report["esim"] = esim
    report["checks"]["euicc"] = esim.get("euicc_supported") is True or esim.get("euicc_enabled") is True
    report["checks"]["no_real_esim"] = esim.get("real_esim_enabled") is False

    provision = request(
        serial,
        args.adb,
        "mobi_rent.provisioning",
        {
            "command": "provision_esim",
            "job_id": "dry-run-slot-1",
            "slot_id": args.slot,
            "activation_code": "LPA:1$DRY_RUN_NOT_A_REAL_CODE",
        },
        args.timeout,
    )
    report["provision_esim"] = provision
    report["checks"]["provision_dry_run"] = (
        provision.get("success") is False and "dry_run" in str(provision.get("error", ""))
    )

    socks = request(
        serial,
        args.adb,
        "mobi_rent.network",
        {"command": "ensure_socks5_route", "slot_id": args.slot, "host": "127.0.0.1", "port": 1080},
        args.timeout,
    )
    report["socks"] = socks
    report["checks"]["socks_dry_run"] = socks.get("success") is True and socks.get("active") is False

    vpn_start = request(serial, args.adb, "mobi_rent.network", {"command": "start_test_vpn"}, args.timeout)
    report["vpn_start"] = vpn_start
    report["checks"]["vpn_test"] = vpn_start.get("dry_run") is True or vpn_start.get("consent_required") is True
    vpn_stop = request(serial, args.adb, "mobi_rent.network", {"command": "stop_vpn"}, args.timeout)
    report["vpn_stop"] = vpn_stop
    report["checks"]["vpn_stop"] = vpn_stop.get("success") is True

    foreign = request(
        serial,
        args.adb,
        "mobi_rent.companion",
        {"command": "get_health", "slot_id": 20 if args.slot != 20 else 1},
        args.timeout,
    )
    report["foreign_slot"] = foreign
    report["checks"]["rejects_other_slot"] = foreign.get("success") is False

    print(json.dumps(report, indent=2))
    failed = [name for name, ok in report["checks"].items() if not ok]
    if failed:
        print("FAILED checks: " + ", ".join(failed), file=sys.stderr)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
