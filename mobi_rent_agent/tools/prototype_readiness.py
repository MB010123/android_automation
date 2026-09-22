"""Isolated PhoneFarmBox prototype runner.

Never loads production VoidFix into the farm daemon. Never writes
slot_map.json. Never sends SMS unless every real-send gate passes.

Usage (from mobi_rent_agent):

    python tools/prototype_readiness.py --env prototype audit
    python tools/prototype_readiness.py --env prototype dry-run --device prototype-device-1
    python tools/prototype_readiness.py --env prototype device-status --device prototype-device-1
    python tools/prototype_readiness.py --env prototype esim-status --device prototype-device-1
    python tools/prototype_readiness.py --env prototype voidfix-status
    python tools/prototype_readiness.py --env prototype send-test-sms --device prototype-device-1 --confirm
    python tools/prototype_readiness.py --env prototype report --device prototype-device-1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.prototype_service import PrototypeRefuse, PrototypeService
from infrastructure.adb_companion import AdbCommandRunner
from infrastructure.adb_four_layer import collect_four_layer_verification
from infrastructure.android_authorization_probe import AdbAuthorizationProbe
from infrastructure.prototype_audit import PrototypeAuditStore
from infrastructure.prototype_config import (
    DEFAULT_DEVICES_FILE,
    DEFAULT_ENV_FILE,
    DEFAULT_SLOT_MAP,
    PROTOTYPE_DIR,
    PrototypeConfigError,
    load_prototype_config,
)
from infrastructure.prototype_dpc import handlers_declared_in_manifest
from infrastructure.prototype_voidfix_api import (
    PrototypeVoidFixRestClient,
    live_android_sim_slot_index,
)

COMPANION_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "android_companion"
    / "app"
    / "src"
    / "main"
    / "AndroidManifest.xml"
)


def _resolve_devices(path: str) -> str:
    candidate = Path(path)
    if candidate.exists():
        return str(candidate)
    example = PROTOTYPE_DIR / "prototype_devices.example.json"
    if example.exists():
        return str(example)
    return path


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _load_service(args: argparse.Namespace) -> PrototypeService:
    if args.env != "prototype":
        raise PrototypeRefuse("prototype runner requires --env prototype")
    config = load_prototype_config(
        env_file=args.prototype_env if Path(args.prototype_env).exists() else None,
        devices_file=_resolve_devices(args.devices),
        slot_map_path=args.slot_map,
    )
    audit = PrototypeAuditStore(config.log_dir / "audit.jsonl")
    gateway = None
    if (
        config.voidfix_enabled
        and config.voidfix_api_key
        and args.command == "send-test-sms"
    ):
        gateway = PrototypeVoidFixRestClient(
            api_key=config.voidfix_api_key,
            send_endpoint=config.voidfix_send_endpoint,
            inbound_endpoint=config.voidfix_inbound_endpoint,
        )
    return PrototypeService(config, gateway=gateway, audit=audit)


def _handlers() -> dict[str, bool]:
    if COMPANION_MANIFEST.exists():
        return handlers_declared_in_manifest(COMPANION_MANIFEST.read_text(encoding="utf-8"))
    return {}


def _live_probe(service: PrototypeService, device_id: str):
    """Read-only ADB diagnostics for the mapped prototype device only."""
    device = service._config.devices.get(device_id)
    if device is None or not device.adb_serial:
        return None, "", None
    if device.adb_serial in service._config.production_serials:
        raise PrototypeRefuse("refusing to probe a production farm serial")
    runner = AdbCommandRunner("adb", 15.0)
    snapshot = AdbAuthorizationProbe(runner).read(device.adb_serial)
    try:
        dump = runner.run(device.adb_serial, ["shell", "dumpsys", "device_policy"]).stdout
    except Exception:
        dump = ""
    try:
        verification = collect_four_layer_verification(runner, device.adb_serial)
    except Exception:
        verification = None
    return snapshot, dump, verification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated PhoneFarmBox prototype runner")
    parser.add_argument("--env", required=True, help="Must be 'prototype'")
    parser.add_argument("--prototype-env", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--devices", default=str(DEFAULT_DEVICES_FILE))
    parser.add_argument("--slot-map", default=str(DEFAULT_SLOT_MAP), help="Read-only production serial deny-list")
    parser.add_argument("--device", default="prototype-device-1")
    parser.add_argument("--recipient", default="")
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument(
        "command",
        choices=[
            "audit",
            "dry-run",
            "device-status",
            "esim-status",
            "voidfix-status",
            "send-test-sms",
            "report",
        ],
    )
    args = parser.parse_args(argv)

    try:
        service = _load_service(args)
        if args.command == "audit":
            _print_json(service.audit())
            return 0
        if args.command == "voidfix-status":
            _print_json(service.voidfix_status())
            return 0
        if args.command == "device-status":
            snapshot, dump, _verification = _live_probe(service, args.device)
            _print_json(
                service.device_status(
                    args.device,
                    snapshot=snapshot,
                    device_policy_dump=dump,
                    handlers=_handlers(),
                )
            )
            return 0
        if args.command == "esim-status":
            snapshot, _dump, verification = _live_probe(service, args.device)
            _print_json(
                service.esim_status(
                    args.device,
                    snapshot=snapshot,
                    verification=verification,
                )
            )
            return 0
        if args.command == "dry-run":
            snapshot, _dump, verification = _live_probe(service, args.device)
            _print_json(service.dry_run(args.device, verification=verification))
            return 0
        if args.command == "report":
            snapshot, dump, verification = _live_probe(service, args.device)
            payload = service.report(
                args.device,
                snapshot=snapshot,
                verification=verification,
                device_policy_dump=dump,
            )
            payload["device_status"]["provisioning_handlers"] = _handlers() or payload["device_status"].get(
                "provisioning_handlers"
            )
            _print_json(payload)
            return 0
        if args.command == "send-test-sms":
            preview = service.preview_send(args.device, args.recipient)
            for key, value in preview.items():
                print(f"{key}: {value}")
            _snapshot, _dump, verification = _live_probe(service, args.device)
            result = service.send_test_sms(
                args.device,
                args.recipient,
                confirm=args.confirm,
                operation_id=args.operation_id or None,
                android_sim_slot_index=live_android_sim_slot_index(verification),
            )
            _print_json(result)
            return 0 if result.get("sms_sent") else 2
    except (PrototypeRefuse, PrototypeConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
