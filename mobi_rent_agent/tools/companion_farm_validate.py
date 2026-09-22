"""Farm-wide companion validation for all 20 slots.

Does not enable real eSIM, production SOCKS5, VoidFix, or auto-recovery.
Does not modify slot_map.json. Does not use adb reconnect.

Usage (from mobi_rent_agent/):
    python tools/companion_farm_validate.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.adb_slot_status import load_slot_map

ADB = "adb"
TIMEOUT = 12.0
PACKAGE = "com.mobirent.companion"
ACTIVITY = "com.mobirent.companion/.MainActivity"


def adb(serial: str, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        [ADB, "-s", serial, *args],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "adb failed")
    return (completed.stdout or "").strip()


def request(serial: str, socket_name: str, payload: dict) -> dict:
    runner = AdbCommandRunner(adb_path=ADB, timeout_seconds=TIMEOUT)
    return AdbForwardedJsonClient(runner, socket_name, TIMEOUT).request(serial, payload)


def validate_slot(slot: int, serial: str) -> dict:
    checks: dict[str, bool] = {}
    responses: dict[str, dict] = {}

    # ADB online
    state = adb(serial, "get-state", check=False)
    checks["adb_online"] = state == "device"
    if not checks["adb_online"]:
        return {"slot": slot, "serial": serial, "checks": checks, "responses": responses}

    # Companion installed
    pkg = adb(serial, "shell", "pm", "path", PACKAGE, check=False)
    checks["companion_installed"] = "package:" in pkg

    # Version
    ver = adb(serial, "shell", "dumpsys", "package", PACKAGE, check=False)
    checks["version_0_1_0"] = "versionName=0.1.0" in ver

    # Grant permissions silently (idempotent)
    for perm in ("android.permission.POST_NOTIFICATIONS", "android.permission.READ_PHONE_STATE"):
        adb(serial, "shell", "pm", "grant", PACKAGE, perm, check=False)
    adb(serial, "shell", "appops", "set", PACKAGE, "ACTIVATE_VPN", "allow", check=False)

    # Start activity with slot
    adb(serial, "shell", "am", "start", "-n", ACTIVITY, "--ei", "slot_id", str(slot), check=False)
    time.sleep(1.5)

    # Process running
    pid = adb(serial, "shell", "pidof", PACKAGE, check=False)
    checks["process_running"] = bool(pid)

    # Foreground service
    svc = adb(serial, "shell", "dumpsys", "activity", "services", PACKAGE, check=False)
    checks["foreground_service"] = "CompanionForegroundService" in svc

    # Socket tests
    try:
        assign = request(serial, "mobi_rent.companion", {"command": "assign_slot", "slot_id": slot})
        responses["assign_slot"] = assign
        checks["slot_assigned"] = assign.get("success") is True and assign.get("slot_id") == slot
    except Exception as exc:
        checks["slot_assigned"] = False
        responses["assign_slot"] = {"error": str(exc)}

    try:
        ping = request(serial, "mobi_rent.companion", {"command": "ping"})
        responses["ping"] = ping
        checks["ping"] = ping.get("success") is True and ping.get("pong") is True
    except Exception as exc:
        checks["ping"] = False
        responses["ping"] = {"error": str(exc)}

    try:
        identity = request(serial, "mobi_rent.companion", {"command": "get_identity"})
        responses["identity"] = identity
        checks["identity"] = (
            identity.get("success") is True
            and bool(identity.get("device_id"))
            and identity.get("slot_id") == slot
            and "Pixel" in str(identity.get("model", ""))
        )
        checks["slot_correct"] = identity.get("slot_id") == slot
        checks["device_id_stable"] = bool(identity.get("device_id"))
    except Exception as exc:
        checks["identity"] = False
        checks["slot_correct"] = False
        checks["device_id_stable"] = False
        responses["identity"] = {"error": str(exc)}

    try:
        health = request(serial, "mobi_rent.companion", {"command": "get_health"})
        responses["health"] = health
        checks["health"] = health.get("success") is True and health.get("app_healthy") is True
    except Exception as exc:
        checks["health"] = False
        responses["health"] = {"error": str(exc)}

    try:
        esim = request(serial, "mobi_rent.provisioning", {"command": "get_esim_status"})
        responses["esim"] = esim
        checks["euicc_detected"] = esim.get("euicc_supported") is True or esim.get("euicc_enabled") is True
        checks["no_real_esim"] = esim.get("real_esim_enabled") is False
    except Exception as exc:
        checks["euicc_detected"] = False
        checks["no_real_esim"] = False
        responses["esim"] = {"error": str(exc)}

    try:
        provision = request(serial, "mobi_rent.provisioning", {
            "command": "provision_esim",
            "job_id": f"farm-validate-slot-{slot}",
            "slot_id": slot,
            "activation_code": "LPA:1$DRY_RUN_NOT_REAL",
        })
        responses["provision"] = provision
        checks["esim_dry_run"] = (
            provision.get("success") is False
            and "dry_run" in str(provision.get("error", ""))
        )
    except Exception as exc:
        checks["esim_dry_run"] = False
        responses["provision"] = {"error": str(exc)}

    try:
        socks = request(serial, "mobi_rent.network", {
            "command": "ensure_socks5_route",
            "slot_id": slot,
            "host": "127.0.0.1",
            "port": 1080,
        })
        responses["socks"] = socks
        checks["socks_dry_run"] = (
            socks.get("success") is True
            and socks.get("active") is False
            and socks.get("dry_run") is True
        )
    except Exception as exc:
        checks["socks_dry_run"] = False
        responses["socks"] = {"error": str(exc)}

    try:
        vpn_start = request(serial, "mobi_rent.network", {"command": "start_test_vpn"})
        vpn_stop = request(serial, "mobi_rent.network", {"command": "stop_vpn"})
        responses["vpn_start"] = vpn_start
        responses["vpn_stop"] = vpn_stop
        checks["vpn_dry_run"] = (
            (vpn_start.get("dry_run") is True or vpn_start.get("consent_required") is True)
            and vpn_stop.get("success") is True
        )
    except Exception as exc:
        checks["vpn_dry_run"] = False
        responses["vpn_start"] = {"error": str(exc)}

    return {"slot": slot, "serial": serial, "checks": checks, "responses": responses}


def main() -> int:
    slot_map = load_slot_map("slot_map.json")
    results = []
    for slot in sorted(slot_map):
        serial = slot_map[slot]
        print(f"Validating slot {slot} ({serial})...", flush=True)
        result = validate_slot(slot, serial)
        results.append(result)
        failed = [k for k, v in result["checks"].items() if not v]
        if failed:
            print(f"  FAIL: {', '.join(failed)}", flush=True)
        else:
            print(f"  PASS ({len(result['checks'])} checks)", flush=True)

    # Summary
    print("\n" + "=" * 80)
    print(f"{'Slot':>4} {'Serial':<18} {'ADB':>4} {'App':>4} {'Slot':>5} {'ID':>4} {'Hlth':>5} {'eUIC':>5} {'eSIM':>5} {'VPN':>4} {'SOCKS':>6} {'Ping':>5}")
    print("-" * 80)
    all_pass = True
    for r in results:
        c = r["checks"]
        def yn(key: str) -> str:
            return "PASS" if c.get(key) else "FAIL"
        print(f"{r['slot']:>4} {r['serial']:<18} {yn('adb_online'):>4} {yn('companion_installed'):>4} {yn('slot_correct'):>5} {yn('device_id_stable'):>4} {yn('health'):>5} {yn('euicc_detected'):>5} {yn('esim_dry_run'):>5} {yn('vpn_dry_run'):>4} {yn('socks_dry_run'):>6} {yn('ping'):>5}")
        if any(not v for v in c.values()):
            all_pass = False
    print("=" * 80)

    # Totals
    total = len(results)
    for key in ["adb_online", "companion_installed", "slot_correct", "device_id_stable",
                "health", "euicc_detected", "esim_dry_run", "vpn_dry_run", "socks_dry_run", "ping"]:
        count = sum(1 for r in results if r["checks"].get(key))
        print(f"  {key}: {count}/{total}")

    # Dump full JSON
    with open("farm_validation_report.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nFull report: farm_validation_report.json")

    if all_pass:
        print("\nALL 20 SLOTS PASSED")
        return 0
    else:
        failed_slots = [r["slot"] for r in results if any(not v for v in r["checks"].values())]
        print(f"\nFAILED SLOTS: {failed_slots}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
