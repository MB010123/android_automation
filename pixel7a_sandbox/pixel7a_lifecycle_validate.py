"""Repeatable Pixel 7a #1 lifecycle validation.

Serial 3C071JEHN14705 only. Does not load the farm bay serial file.
Does not start main.py. Activation codes are never printed.

Stages:
  1. pre-state — any enabled embedded profile (carrier-agnostic)
  2. download + switch-after — only if no active profile, or --force-download
  3. post-state + telephony registry HOME

Usage (from repo root or this directory):

    python pixel7a_sandbox/pixel7a_lifecycle_validate.py --serial 3C071JEHN14705
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


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Pixel 7a #1 lifecycle validation")
    parser.add_argument("--serial", default=PIXEL_7A_1_SERIAL)
    parser.add_argument(
        "--force-download",
        action="store_true",
        default=False,
        help="invoke EuiccManager even if a profile is already enabled",
    )
    args = parser.parse_args()
    try:
        serial = require_pixel7a_1(args.serial)
    except ValueError as exc:
        print(json.dumps(public_sandbox_report({"success": False, "error": str(exc)})))
        return 2
    if load_dotenv is not None:
        load_dotenv(AGENT_ROOT / ".env", override=False)

    runner = AdbCommandRunner("adb", 180)
    client = AdbForwardedJsonClient(runner, "mobi_rent.provisioning", 180)
    probe = AdbAuthorizationProbe(runner, client, real_esim_flag=True)
    snapshot = probe.read(serial)
    caps = derive_esim_capabilities(snapshot)
    before = collect_sandbox_profile(runner, serial)
    before_ok, before_reason = switch_after_download_reached_enabled(before)
    report = {
        "target": "pixel7a_1",
        "serial": serial,
        "device_owner": snapshot.device_owner,
        "can_download": caps.can_download,
        "authorization_source": caps.authorization_source,
        "pre_state": before.to_public_dict(),
        "download_attempted": False,
        "download_skipped": None,
        "switch_after_download": True,
        "post_state": None,
        "switch_after_validated": False,
        "registry_home": False,
        "success": False,
    }

    need_download = args.force_download or not before_ok
    if not need_download:
        report["download_skipped"] = "already_enabled"
        report["post_state"] = before.to_public_dict()
        report["switch_after_validated"] = True
        report["registry_home"] = bool(before.registry_home)
        report["success"] = True
        print(json.dumps(public_sandbox_report(report), indent=2))
        return 0

    armed = sandbox_live_download_armed(
        real_esim_enabled=_truthy(os.getenv("REAL_ESIM_ENABLED", "")),
        esim_live_download_armed=_truthy(os.getenv("ESIM_LIVE_DOWNLOAD_ARMED", "")),
    )
    if not armed:
        report["error"] = "sandbox live download not armed"
        report["post_state"] = before.to_public_dict()
        print(json.dumps(public_sandbox_report(report), indent=2))
        return 3
    if not caps.unattended or not caps.can_download or caps.authorization_source == "none":
        report["error"] = caps.reason
        report["post_state"] = before.to_public_dict()
        print(json.dumps(public_sandbox_report(report), indent=2))
        return 3
    code = _activation_code()
    if not code:
        report["error"] = "activation code unset; EuiccManager download was not invoked"
        report["post_state"] = before.to_public_dict()
        print(json.dumps(public_sandbox_report(report), indent=2))
        return 4

    isolation = SlotIsolationPolicy({COMPANION_PROTOCOL_SLOT})
    provider = AuthorizedEsimProvider(
        probe,
        isolation,
        live_download_armed=True,
        download_transport=companion_provision_transport(client),
        verification_source=lambda ser, job: collect_four_layer_verification(
            runner, ser, client.request(ser, {"command": "get_esim_status"})
        ),
    )
    job = ActivationJob(
        f"pixel7a-lifecycle-{int(time.time())}",
        COMPANION_PROTOCOL_SLOT,
        code,
        switch_after_download=True,
    )
    result = provider.provision(serial, job)
    report["download_attempted"] = True
    report["download_error"] = result.error
    after = collect_sandbox_profile(runner, serial)
    ok, reason = switch_after_download_reached_enabled(after)
    report["post_state"] = after.to_public_dict()
    report["switch_after_validated"] = ok
    report["registry_home"] = bool(after.registry_home)
    report["success"] = ok and after.registry_home
    report["error"] = None if report["success"] else (reason or result.error or "registry HOME not observed")
    print(json.dumps(public_sandbox_report(report), indent=2))
    return 0 if report["success"] else 5


if __name__ == "__main__":
    sys.exit(main())
