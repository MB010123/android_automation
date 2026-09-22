"""Slot 1 farm live eSIM download + four-layer verification.

Farm Slot 1 only (serial from slot_map.json). Does not target Pixel 7a
sandbox devices. Does not start the farm daemon. Activation codes are
never printed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_GATE_PATH = Path(__file__).with_name("slot1_euicc_slot_gate.py")
_GATE_SPEC = importlib.util.spec_from_file_location("slot1_euicc_slot_gate", _GATE_PATH)
assert _GATE_SPEC is not None and _GATE_SPEC.loader is not None
_GATE = importlib.util.module_from_spec(_GATE_SPEC)
sys.modules["slot1_euicc_slot_gate"] = _GATE
_GATE_SPEC.loader.exec_module(_GATE)

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
    live_download_may_arm,
)
from infrastructure.adb_slot_status import load_slot_map

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

SLOT = 1
SERIAL = "1C101FDF6009EZ"
MAP_PATH = Path(__file__).resolve().parents[1] / "slot_map.json"


def resolve_live_download_serial(
    slot_map: dict[int, str],
    *,
    farm_slot1_serial: str = SERIAL,
) -> tuple[str, str | None]:
    """Farm Slot 1 only. Pixel 7a sandbox has its own tool."""
    serial = slot_map.get(SLOT)
    if serial != farm_slot1_serial:
        return "", "slot 1 serial mismatch; refusing"
    return serial, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Farm Slot 1 live eSIM download")
    parser.add_argument(
        "--switch-after-download",
        action="store_true",
        default=False,
        help="ask EuiccManager to enable the profile after download; default false",
    )
    args = parser.parse_args()
    if load_dotenv is not None:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    isolation = SlotIsolationPolicy({SLOT})
    isolation.reject_if_outside(SLOT)
    slot_map = load_slot_map(MAP_PATH)
    serial, serial_error = resolve_live_download_serial(slot_map)
    if serial_error:
        print(json.dumps({"success": False, "error": serial_error}))
        return 2
    for other in range(2, 21):
        if other in isolation.allowed_slot_ids:
            print(json.dumps({"success": False, "error": "allowlist is not slot 1 only"}))
            return 2
    armed = live_download_may_arm(
        real_esim_enabled=os.getenv("REAL_ESIM_ENABLED", "").lower() in {"1", "true", "yes", "on"},
        esim_live_download_armed=os.getenv("ESIM_LIVE_DOWNLOAD_ARMED", "").lower() in {"1", "true", "yes", "on"},
        allowed_slot_ids=isolation.allowed_slot_ids,
    )
    runner = AdbCommandRunner("adb", 180)
    client = AdbForwardedJsonClient(runner, "mobi_rent.provisioning", 180)
    probe = AdbAuthorizationProbe(runner, client, real_esim_flag=True)
    snapshot = probe.read(serial)
    caps = derive_esim_capabilities(snapshot)
    report = {
        "slot_id": SLOT,
        "serial": serial,
        "device_owner": snapshot.device_owner,
        "unattended": caps.unattended,
        "can_download": caps.can_download,
        "can_switch": caps.can_switch,
        "can_delete": caps.can_delete,
        "authorization_source": caps.authorization_source,
        "live_download_armed": armed,
        "switch_after_download": bool(args.switch_after_download),
        "activation_code_sent": False,
        "download_attempted": False,
        "success": False,
    }
    if not armed:
        report["error"] = "live download not armed (need REAL_ESIM_ENABLED, ESIM_LIVE_DOWNLOAD_ARMED, allowlist {1})"
        print(json.dumps(report, indent=2))
        return 3
    if not caps.unattended or not caps.can_download or caps.authorization_source == "none":
        report["error"] = caps.reason
        print(json.dumps(report, indent=2))
        return 3
    observation = _GATE.collect_euicc_slot_observation(runner, serial)
    ready, gate_reason = _GATE.evaluate_slot_gate(observation)
    report["euicc_slot_gate"] = observation.to_public_dict()
    report["euicc_slot_gate_ready"] = ready
    if not ready:
        report["error"] = gate_reason
        print(json.dumps(report, indent=2))
        return 3
    code = (os.getenv("SLOT1_ACTIVATION_CODE") or "").strip()
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
        snapshot_layers = collect_four_layer_verification(
            runner, serial, client.request(serial, {"command": "get_esim_status"})
        )
        verified = provider.verify_profile(serial, ActivationJob("slot1-verify", SLOT, qr_url="https://local.test/none"), snapshot_layers)
        report["verdict"] = verified.verdict.value if verified.verdict else None
        report["success"] = verified.success
        report["error"] = (
            "SLOT1_ACTIVATION_CODE is unset; EuiccManager download was not invoked. "
            + (verified.error or "")
        )
        print(json.dumps(report, indent=2))
        return 4 if verified.verdict is not ActivationVerdict.ACTIVATION_CONFIRMED else 0
    job = ActivationJob(
        "slot1-live",
        SLOT,
        code,
        switch_after_download=bool(args.switch_after_download),
    )
    result = provider.provision(serial, job)
    report["download_attempted"] = True
    report["activation_code_sent"] = True
    report["verdict"] = result.verdict.value if result.verdict else None
    report["success"] = result.success
    report["error"] = result.error
    print(json.dumps(report, indent=2))
    return 0 if result.success and result.verdict is ActivationVerdict.ACTIVATION_CONFIRMED else 5


if __name__ == "__main__":
    sys.exit(main())
