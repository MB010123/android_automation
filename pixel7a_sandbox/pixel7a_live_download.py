"""Pixel 7a #1 sandbox live eSIM download.

Canonical copy: pixel7a_sandbox/pixel7a_live_download.py
Targets only serial 3C071JEHN14705. Does not load the farm bay serial file.
Does not talk to farm bays or Pixel 7a #2. Does not start main.py.
Activation codes are never printed.

When --switch-after-download is set, success requires any enabled
embedded profile with state=1 and a mapped SIM slot.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1] / "mobi_rent_agent"
sys.path.insert(0, str(AGENT_ROOT))

from domain.esim_capabilities import derive_esim_capabilities
from domain.models import ActivationJob
from domain.provisioning_state import ActivationVerdict
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.adb_companion import AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.adb_four_layer import collect_four_layer_verification
from infrastructure.android_authorization_probe import AdbAuthorizationProbe
from infrastructure.authorized_esim_provider import (
    AuthorizedEsimProvider,
    companion_provision_transport,
)
from infrastructure.sandbox_device import (
    COMPANION_PROTOCOL_SLOT,
    PIXEL_7A_1_SERIAL,
    public_sandbox_report,
    require_pixel7a_1,
    sandbox_live_download_armed,
)
from infrastructure.tello_profile_state import (
    collect_sandbox_profile,
    switch_after_download_reached_enabled,
)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _activation_code() -> str:
    for key in ("PIXEL7A_ACTIVATION_CODE", "SLOT1_ACTIVATION_CODE"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Pixel 7a #1 sandbox live eSIM download")
    parser.add_argument("--serial", default=PIXEL_7A_1_SERIAL)
    parser.add_argument(
        "--switch-after-download",
        action="store_true",
        default=False,
        help="enable the profile after download; validate LPA state=1",
    )
    args = parser.parse_args()
    try:
        serial = require_pixel7a_1(args.serial)
    except ValueError as exc:
        print(json.dumps(public_sandbox_report({"success": False, "error": str(exc)})))
        return 2
    if load_dotenv is not None:
        load_dotenv(AGENT_ROOT / ".env", override=False)
    armed = sandbox_live_download_armed(
        real_esim_enabled=os.getenv("REAL_ESIM_ENABLED", "").lower() in {"1", "true", "yes", "on"},
        esim_live_download_armed=os.getenv("ESIM_LIVE_DOWNLOAD_ARMED", "").lower() in {"1", "true", "yes", "on"},
    )
    isolation = SlotIsolationPolicy({COMPANION_PROTOCOL_SLOT})
    runner = AdbCommandRunner("adb", 180)
    client = AdbForwardedJsonClient(runner, "mobi_rent.provisioning", 180)
    probe = AdbAuthorizationProbe(runner, client, real_esim_flag=True)
    snapshot = probe.read(serial)
    caps = derive_esim_capabilities(snapshot)
    report = {
        "target": "pixel7a_1",
        "serial": serial,
        "device_owner": snapshot.device_owner,
        "unattended": caps.unattended,
        "can_download": caps.can_download,
        "authorization_source": caps.authorization_source,
        "live_download_armed": armed,
        "switch_after_download": bool(args.switch_after_download),
        "activation_code_sent": False,
        "download_attempted": False,
        "profile_state": None,
        "switch_after_validated": False,
        "success": False,
    }
    if not armed:
        report["error"] = "sandbox live download not armed"
        print(json.dumps(public_sandbox_report(report), indent=2))
        return 3
    if not caps.unattended or not caps.can_download or caps.authorization_source == "none":
        report["error"] = caps.reason
        print(json.dumps(public_sandbox_report(report), indent=2))
        return 3
    code = _activation_code()
    provider = AuthorizedEsimProvider(
        probe,
        isolation,
        live_download_armed=True,
        download_transport=companion_provision_transport(client),
        verification_source=lambda ser, job: collect_four_layer_verification(
            runner, ser, client.request(ser, {"command": "get_esim_status"})
        ),
    )
    if not code:
        profile = collect_sandbox_profile(runner, serial)
        report["profile_state"] = profile.to_public_dict()
        report["error"] = "activation code unset; EuiccManager download was not invoked"
        print(json.dumps(public_sandbox_report(report), indent=2))
        return 4
    job = ActivationJob(
        f"pixel7a-live-{int(time.time())}",
        COMPANION_PROTOCOL_SLOT,
        code,
        switch_after_download=bool(args.switch_after_download),
    )
    result = provider.provision(serial, job)
    report["download_attempted"] = True
    report["activation_code_sent"] = True
    report["verdict"] = result.verdict.value if result.verdict else None
    report["download_error"] = result.error
    profile = collect_sandbox_profile(runner, serial)
    report["profile_state"] = profile.to_public_dict()
    if args.switch_after_download:
        ok, reason = switch_after_download_reached_enabled(profile)
        report["switch_after_validated"] = ok
        report["four_layer_verdict"] = result.verdict.value if result.verdict else None
        report["success"] = ok
        report["error"] = None if ok else (reason or result.error)
        print(json.dumps(public_sandbox_report(report), indent=2))
        return 0 if ok else 5
    report["success"] = bool(result.success and result.verdict is ActivationVerdict.ACTIVATION_CONFIRMED)
    report["error"] = result.error
    print(json.dumps(public_sandbox_report(report), indent=2))
    return 0 if report["success"] else 5


if __name__ == "__main__":
    sys.exit(main())
