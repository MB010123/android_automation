"""Install the companion APK onto farm phones after the single-device test.

Does not enable provisioning, production SOCKS5, health reboot, or VoidFix.
Does not modify slot_map.json.

Usage (from mobi_rent_agent/):

    python tools/companion_deploy.py --apk ..\\android_companion\\app\\build\\outputs\\apk\\debug\\app-debug.apk
    python tools/companion_deploy.py --apk PATH --slots 1
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_slot_status import load_slot_map

PACKAGE_ACTIVITY = "com.mobirent.companion/.MainActivity"


def install_one(serial: str, slot: int, apk: Path, adb_path: str) -> None:
    commands = [
        [adb_path, "-s", serial, "install", "-r", str(apk)],
        [adb_path, "-s", serial, "shell", "pm", "grant", "com.mobirent.companion", "android.permission.POST_NOTIFICATIONS"],
        [adb_path, "-s", serial, "shell", "pm", "grant", "com.mobirent.companion", "android.permission.READ_PHONE_STATE"],
        [
            adb_path,
            "-s",
            serial,
            "shell",
            "am",
            "start",
            "-n",
            PACKAGE_ACTIVITY,
            "--ei",
            "slot_id",
            str(slot),
        ],
    ]
    for command in commands:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        if completed.returncode != 0 and "pm" not in command:
            raise RuntimeError(f"{serial}: {' '.join(command)} failed: {completed.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy companion APK to mapped slots")
    parser.add_argument("--apk", required=True)
    parser.add_argument("--slot-map", default="slot_map.json")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--slots", default="", help="comma-separated slot ids; default is all mapped slots")
    args = parser.parse_args()

    apk = Path(args.apk)
    if not apk.is_file():
        print(f"APK not found: {apk}", file=sys.stderr)
        return 1

    slot_map = load_slot_map(args.slot_map)
    slots = [int(part) for part in args.slots.split(",") if part.strip()] or sorted(slot_map)
    failures = 0
    for slot in slots:
        serial = slot_map[slot]
        try:
            install_one(serial, slot, apk, args.adb)
            print(f"ok slot {slot} {serial}")
        except Exception as exc:
            failures += 1
            print(f"FAIL slot {slot} {serial}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
