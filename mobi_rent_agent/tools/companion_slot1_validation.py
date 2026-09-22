"""Slot 1-only companion validation. Does not touch slots 2-20 or slot_map.json."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.adb_slot_status import load_slot_map

PACKAGE = "com.mobirent.companion"
ACTIVITY = "com.mobirent.companion/.MainActivity"
SERIAL = None
ADB = "adb"
TIMEOUT = 10.0


def adb(*args: str, check: bool = True) -> str:
    completed = subprocess.run(
        [ADB, "-s", SERIAL, *args],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "adb failed")
    return (completed.stdout or "").strip()


def request(socket_name: str, payload: dict) -> dict:
    runner = AdbCommandRunner(adb_path=ADB, timeout_seconds=TIMEOUT)
    return AdbForwardedJsonClient(runner, socket_name, TIMEOUT).request(SERIAL, payload)


def main() -> int:
    global SERIAL
    slot_map = load_slot_map("slot_map.json")
    SERIAL = slot_map[1]
    apk = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        r"..\android_companion\app\build\outputs\apk\debug\app-debug.apk"
    )
    report: dict = {"serial": SERIAL, "checks": {}, "responses": {}}

    others = [adb_other(slot_map[slot], "shell", "pm", "path", PACKAGE) for slot in range(2, 21)]
    report["checks"]["slots_2_20_untouched_before"] = all("package:" not in body for body in others)

    adb("install", "-r", str(apk.resolve()))
    report["checks"]["apk_installed"] = "package:" in adb("shell", "pm", "path", PACKAGE)
    for permission in (
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.READ_PHONE_STATE",
    ):
        adb("shell", "pm", "grant", PACKAGE, permission, check=False)
    adb("shell", "appops", "set", PACKAGE, "ACTIVATE_VPN", "allow", check=False)

    adb("shell", "am", "start", "-n", ACTIVITY, "--ei", "slot_id", "1")
    time.sleep(2)
    pids = [adb("shell", "pidof", PACKAGE, check=False) for _ in range(4)]
    time.sleep(2)
    pids.append(adb("shell", "pidof", PACKAGE, check=False))
    report["pids_after_start"] = pids
    report["checks"]["app_started"] = all(pids) and len({p.split()[0] for p in pids if p}) == 1

    fgs = adb("shell", "dumpsys", "activity", "services", PACKAGE)
    report["checks"]["foreground_service"] = (
        "CompanionForegroundService" in fgs and "isForeground=true" in fgs.replace(" ", "")
    ) or ("CompanionForegroundService" in fgs)

    pkg = adb("shell", "dumpsys", "package", PACKAGE)
    report["checks"]["boot_receiver"] = "com.mobirent.companion/.BootReceiver" in pkg and (
        "BOOT_COMPLETED" in pkg or "BootReceiver" in pkg
    )

    crash = adb("logcat", "-d", "-t", "80", "--pid", pids[-1].split()[0] if pids[-1] else "0", check=False)
    report["checks"]["no_crash_loop"] = "FATAL EXCEPTION" not in crash and report["checks"]["app_started"]

    assigned = request("mobi_rent.companion", {"command": "assign_slot", "slot_id": 1})
    ping = request("mobi_rent.companion", {"command": "ping"})
    identity = request("mobi_rent.companion", {"command": "get_identity"})
    health = request("mobi_rent.companion", {"command": "get_health"})
    esim = request("mobi_rent.provisioning", {"command": "get_esim_status"})
    provision = request(
        "mobi_rent.provisioning",
        {
            "command": "provision_esim",
            "job_id": "slot1-validation-dry-run",
            "slot_id": 1,
            "activation_code": "LPA:1$DRY_RUN_NOT_A_REAL_CODE",
        },
    )
    socks = request(
        "mobi_rent.network",
        {"command": "ensure_socks5_route", "slot_id": 1, "host": "127.0.0.1", "port": 1080},
    )
    vpn_status = request("mobi_rent.network", {"command": "vpn_status"})
    vpn_start = request("mobi_rent.network", {"command": "start_test_vpn"})
    vpn_stop = request("mobi_rent.network", {"command": "stop_vpn"})
    report["responses"] = {
        "assign_slot": assigned,
        "ping": ping,
        "identity": identity,
        "health": health,
        "esim": esim,
        "provision_esim": provision,
        "socks": socks,
        "vpn_status": vpn_status,
        "vpn_start": vpn_start,
        "vpn_stop": vpn_stop,
    }
    device_id = identity.get("device_id")
    report["checks"]["ping"] = ping.get("success") is True and ping.get("pong") is True
    report["checks"]["identity"] = (
        identity.get("success") is True
        and identity.get("slot_id") == 1
        and identity.get("model") == "Pixel 6"
        and bool(device_id)
    )
    report["checks"]["health"] = health.get("success") is True and health.get("app_healthy") is True
    report["checks"]["euicc"] = esim.get("euicc_supported") is True and esim.get("euicc_enabled") is True
    report["checks"]["esim_dry_run"] = (
        esim.get("real_esim_enabled") is False
        and provision.get("success") is False
        and "dry_run" in str(provision.get("error", ""))
    )
    report["checks"]["vpn_dry_run"] = (
        socks.get("success") is True
        and socks.get("active") is False
        and socks.get("dry_run") is True
        and (vpn_start.get("dry_run") is True or vpn_start.get("consent_required") is True)
        and vpn_stop.get("success") is True
    )

    adb("shell", "am", "force-stop", PACKAGE)
    time.sleep(1)
    stopped_pid = adb("shell", "pidof", PACKAGE, check=False)
    restart_failed = False
    try:
        request("mobi_rent.companion", {"command": "ping"})
    except (AdbCommandError, OSError, ValueError):
        restart_failed = True
    adb("shell", "am", "start", "-n", ACTIVITY, "--ei", "slot_id", "1")
    time.sleep(2)
    recovered = request("mobi_rent.companion", {"command": "get_identity"})
    report["checks"]["process_restart"] = (
        not stopped_pid
        and restart_failed
        and recovered.get("device_id") == device_id
        and recovered.get("slot_id") == 1
    )
    report["responses"]["identity_after_restart"] = recovered

    # Do not call `adb reconnect` on this USB farm: it can drop the ADB
    # interface while Windows still shows the Pixel, and recovering it
    # requires a host ADB server restart that briefly hits every slot.
    state = adb("get-state", check=False)
    after_adb = request("mobi_rent.companion", {"command": "ping"}) if state == "device" else {}
    report["responses"]["ping_after_adb_available"] = after_adb
    report["checks"]["adb_reconnect"] = state == "device" and after_adb.get("success") is True

    others_after = [adb_other(slot_map[slot], "shell", "pm", "path", PACKAGE) for slot in range(2, 21)]
    report["checks"]["slots_2_20_untouched_after"] = all("package:" not in body for body in others_after)
    other_states = {slot: adb_other(slot_map[slot], "get-state") for slot in range(2, 21)}
    report["other_slot_states"] = other_states
    report["checks"]["slots_2_20_adb_device"] = all(state == "device" for state in other_states.values())

    print(json.dumps(report, indent=2))
    failed = [name for name, ok in report["checks"].items() if not ok]
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        return 1
    print("SLOT 1 VALIDATION PASSED")
    return 0


def adb_other(serial: str, *args: str) -> str:
    completed = subprocess.run(
        [ADB, "-s", serial, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return (completed.stdout or completed.stderr or "").strip()


if __name__ == "__main__":
    sys.exit(main())
