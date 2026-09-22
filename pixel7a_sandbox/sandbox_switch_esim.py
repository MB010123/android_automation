"""Enable an already-downloaded sandbox profile on Pixel 7a #1.

Canonical copy: pixel7a_sandbox/sandbox_switch_esim.py
Does not send an activation code. Does not load the farm bay serial file.
Does not start main.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1] / "mobi_rent_agent"
sys.path.insert(0, str(AGENT_ROOT))

from infrastructure.adb_companion import AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.sandbox_device import (
    COMPANION_PROTOCOL_SLOT,
    PIXEL_7A_1_SERIAL,
    public_sandbox_report,
    require_pixel7a_1,
)
from infrastructure.tello_profile_state import collect_sandbox_profile


def main() -> int:
    parser = argparse.ArgumentParser(description="Pixel 7a #1 switch_esim without activation code")
    parser.add_argument("--serial", default=PIXEL_7A_1_SERIAL)
    parser.add_argument("--subscription-id", type=int, default=None)
    args = parser.parse_args()
    try:
        serial = require_pixel7a_1(args.serial)
    except ValueError as exc:
        print(json.dumps(public_sandbox_report({"success": False, "error": str(exc)})))
        return 2
    runner = AdbCommandRunner("adb", 30)
    profile = collect_sandbox_profile(runner, serial)
    sub_id = args.subscription_id or profile.subscription_id
    report = {
        "target": "pixel7a_1",
        "serial": serial,
        "command": "switch_esim",
        "activation_code_sent": False,
        "subscription_id": sub_id,
        "profile_state": profile.to_public_dict(),
        "success": False,
    }
    if sub_id is None:
        report["error"] = "active subscription id not found; switch not sent"
        print(json.dumps(public_sandbox_report(report), indent=2))
        return 4
    client = AdbForwardedJsonClient(AdbCommandRunner("adb", 180), "mobi_rent.provisioning", 180)
    response = client.request(
        serial,
        {
            "command": "switch_esim",
            "job_id": f"pixel7a-switch-{int(time.time())}",
            "slot_id": COMPANION_PROTOCOL_SLOT,
            "subscription_id": sub_id,
        },
    )
    if isinstance(response, dict):
        response.pop("activation_code", None)
        report["companion"] = response
        report["success"] = response.get("success") is True and response.get("device_code") in (0, None)
        report["error"] = response.get("error")
        after = collect_sandbox_profile(AdbCommandRunner("adb", 30), serial)
        report["profile_state"] = after.to_public_dict()
        if report["success"]:
            report["success"] = after.state == 1 and (
                after.sim_slot_index is None or after.sim_slot_index >= 0
            )
            if not report["success"]:
                report["error"] = f"switch callback ok but profile state={after.state}"
    print(json.dumps(public_sandbox_report(report), indent=2))
    return 0 if report["success"] else 5


if __name__ == "__main__":
    sys.exit(main())
