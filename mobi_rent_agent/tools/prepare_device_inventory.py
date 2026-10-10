"""Build a Farm operator inventory report for slots 1–20.

Joins slot_map + device_registry + slot_msisdn_map. Never writes
public.slots, device_registry, slot_map, or .env. Never restarts services.

Usage (from mobi_rent_agent/):

    python tools/prepare_device_inventory.py
    python tools/prepare_device_inventory.py --probe-imei --probe-companion
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.device_inventory_prepare import (
    CompanionObservation,
    LiveImeiObservation,
    observe_companion_imei_access,
    observe_live_imei,
    prepare_device_inventory,
)
from infrastructure.adb_companion import AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.adb_imei import AdbImeiReader
from infrastructure.adb_slot_status import list_adb_device_states, load_slot_map
from infrastructure.device_registry import load_device_registry
from infrastructure.slot_msisdn_map import load_slot_msisdn_map

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SLOT_MAP = ROOT / "slot_map.json"
DEFAULT_REGISTRY = ROOT / "device_registry.json"
DEFAULT_MSISDN = ROOT / "slot_msisdn_map.json"
DEFAULT_OUTPUT = ROOT / "docs" / "operator_device_inventory_prepare.json"
FARM_HEALTH_URL = "http://127.0.0.1:8790/agent/health"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Farm operator device inventory (no production write)")
    parser.add_argument("--slot-map", default=str(DEFAULT_SLOT_MAP))
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--msisdn-map", default=str(DEFAULT_MSISDN))
    parser.add_argument("--tenant-slots", default=None, help="optional local public.slots fixture JSON")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--probe-imei", action="store_true", help="READ-ONLY AdbImeiReader on mapped serials")
    parser.add_argument(
        "--probe-companion",
        action="store_true",
        help="READ-ONLY companion get_imei_access on mapped serials",
    )
    parser.add_argument("--adb-path", default="adb")
    args = parser.parse_args()

    slot_map = load_slot_map(args.slot_map)
    registry = load_device_registry(args.registry)
    msisdn_path = Path(args.msisdn_map)
    msisdn = load_slot_msisdn_map(msisdn_path) if msisdn_path.exists() else {}
    tenant_slots = _load_optional_json_map(args.tenant_slots)
    adb_states = list_adb_device_states(args.adb_path)
    health = _farm_health()
    live_imei: dict[int, LiveImeiObservation] = {}
    companion: dict[int, CompanionObservation] = {}

    if args.probe_imei:
        reader = AdbImeiReader(AdbCommandRunner(adb_path=args.adb_path, timeout_seconds=15.0))
        for slot_id, serial in sorted(slot_map.items()):
            live_imei[int(slot_id)] = observe_live_imei(serial, reader)

    if args.probe_companion:
        client = AdbForwardedJsonClient(AdbCommandRunner(adb_path=args.adb_path, timeout_seconds=12.0), "mobi_rent.companion", 12.0)
        for slot_id, serial in sorted(slot_map.items()):
            companion[int(slot_id)] = observe_companion_imei_access(serial, client)

    inventory = prepare_device_inventory(
        slot_map=slot_map,
        device_registry=registry,
        farm_msisdn_map=msisdn,
        tenant_slots=tenant_slots,
        live_imei=live_imei,
        companion=companion,
        adb_states=adb_states,
        farm_agent_health=health,
    )
    report = inventory.to_operator_report()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "slot_count": report["slot_count"]}, indent=2))
    return 0


def _farm_health() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(FARM_HEALTH_URL, timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
            if isinstance(body, dict):
                body.pop("serials", None)
                return {"reachable": True, "http_status": response.status, "body": body}
            return {"reachable": True, "http_status": response.status, "body": {"ok": False}}
    except urllib.error.HTTPError as exc:
        return {"reachable": True, "http_status": exc.code, "error": "http_error"}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {"reachable": False, "error": "farm_agent_unreachable"}


def _load_optional_json_map(path: str | None) -> dict[int, dict[str, Any]]:
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        return {}
    raw = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("tenant slots file must be a JSON object keyed by slot_id")
    out: dict[int, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[int(key)] = value
    return out


if __name__ == "__main__":
    sys.exit(main())
