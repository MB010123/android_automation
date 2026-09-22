"""Load the isolated prototype environment. Never writes slot_map.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from domain.prototype import (
    PLACEHOLDER_PREFIX,
    PrototypeMode,
    is_placeholder,
)
from infrastructure.adb_slot_status import SlotMapError, load_slot_map

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

PROTOTYPE_DIR = Path(__file__).resolve().parents[1] / "config" / "prototype"
DEFAULT_ENV_FILE = PROTOTYPE_DIR / "prototype.env"
DEFAULT_DEVICES_FILE = PROTOTYPE_DIR / "prototype_devices.json"
DEFAULT_SLOT_MAP = Path(__file__).resolve().parents[1] / "slot_map.json"
DEFAULT_SEND_ENDPOINT = "https://sms.voidfix.com/services/send.php"


class PrototypeConfigError(RuntimeError):
    """Prototype configuration is missing, invalid, or unsafe."""


@dataclass(frozen=True)
class PrototypeDevice:
    device_id: str
    device_type: str
    adb_serial: str
    voidfix_device_id: str | None
    enabled: bool
    role: str = "test"


@dataclass(frozen=True)
class PrototypeConfig:
    environment: str
    mode: PrototypeMode
    devices: dict[str, PrototypeDevice]
    allowlist: tuple[str, ...]
    recipient_allowlist: tuple[str, ...]
    voidfix_enabled: bool
    voidfix_api_key: str | None
    voidfix_send_endpoint: str
    voidfix_inbound_endpoint: str | None
    voidfix_webhook_secret: str | None
    real_send_confirmation: bool
    log_dir: Path
    production_serials: frozenset[str]
    inbound_documented: bool = False


def load_prototype_config(
    env_file: str | Path | None = DEFAULT_ENV_FILE,
    devices_file: str | Path | None = DEFAULT_DEVICES_FILE,
    slot_map_path: str | Path | None = DEFAULT_SLOT_MAP,
    environ: dict[str, str] | None = None,
) -> PrototypeConfig:
    if environ is not None:
        env = dict(environ)
    else:
        if load_dotenv is not None and env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        env = dict(os.environ)

    environment = (env.get("PROTOTYPE_ENVIRONMENT") or "prototype").strip()
    if environment != "prototype":
        raise PrototypeConfigError("PROTOTYPE_ENVIRONMENT must be 'prototype'")

    mode = _parse_mode(env.get("PROTOTYPE_MODE"))
    devices = load_prototype_devices(devices_file or DEFAULT_DEVICES_FILE)
    allowlist = _parse_csv(env.get("PROTOTYPE_DEVICE_ALLOWLIST"))
    if not allowlist:
        allowlist = tuple(device_id for device_id, device in devices.items() if device.enabled)
    recipient_allowlist = tuple(
        item for item in _parse_csv(env.get("PROTOTYPE_RECIPIENT_ALLOWLIST")) if not is_placeholder(item)
    )
    production_serials = _read_production_serials_readonly(slot_map_path)
    _reject_production_overlap(devices, production_serials)

    log_dir = Path(env.get("PROTOTYPE_LOG_DIR") or (Path(__file__).resolve().parents[1] / "logs" / "prototype"))
    inbound_endpoint = env.get("VOIDFIX_INBOUND_ENDPOINT") or env.get("PROTOTYPE_VOIDFIX_INBOUND_ENDPOINT") or None
    return PrototypeConfig(
        environment=environment,
        mode=mode,
        devices=devices,
        allowlist=allowlist,
        recipient_allowlist=recipient_allowlist,
        voidfix_enabled=_read_bool(env.get("VOIDFIX_ENABLED") or env.get("PROTOTYPE_VOIDFIX_ENABLED"), False),
        voidfix_api_key=(env.get("VOIDFIX_API_KEY") or env.get("PROTOTYPE_VOIDFIX_API_KEY") or None) or None,
        voidfix_send_endpoint=(
            env.get("VOIDFIX_SEND_ENDPOINT")
            or env.get("PROTOTYPE_VOIDFIX_SEND_ENDPOINT")
            or DEFAULT_SEND_ENDPOINT
        ),
        voidfix_inbound_endpoint=inbound_endpoint or None,
        voidfix_webhook_secret=(
            env.get("VOIDFIX_WEBHOOK_SECRET") or env.get("PROTOTYPE_VOIDFIX_WEBHOOK_SECRET") or None
        ),
        real_send_confirmation=_read_bool(env.get("VOIDFIX_REAL_SEND_CONFIRMATION"), False),
        log_dir=log_dir,
        production_serials=production_serials,
        inbound_documented=False,
    )


def load_prototype_devices(path: str | Path) -> dict[str, PrototypeDevice]:
    file_path = Path(path)
    if not file_path.exists():
        raise PrototypeConfigError(f"prototype device map not found: {file_path}")
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrototypeConfigError(f"invalid prototype device map: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise PrototypeConfigError("prototype device map must be a non-empty JSON object")

    devices: dict[str, PrototypeDevice] = {}
    seen_voidfix: dict[str, str] = {}
    seen_serials: dict[str, str] = {}
    for device_id, body in raw.items():
        if not isinstance(body, dict):
            raise PrototypeConfigError(f"{device_id} must be an object")
        serial = str(body.get("adb_serial") or "").strip()
        voidfix = body.get("voidfix_device_id")
        voidfix_id = None if voidfix is None else str(voidfix).strip()
        if voidfix_id == "":
            voidfix_id = None
        if is_placeholder(serial):
            serial = ""
        if voidfix_id and is_placeholder(voidfix_id):
            voidfix_id = None
        if serial and serial in seen_serials:
            raise PrototypeConfigError(f"duplicate ADB serial for {seen_serials[serial]} and {device_id}")
        if voidfix_id:
            previous = seen_voidfix.get(voidfix_id)
            if previous:
                raise PrototypeConfigError(
                    f"duplicate VoidFix device_id shared by {previous} and {device_id}"
                )
            seen_voidfix[voidfix_id] = str(device_id)
        if serial:
            seen_serials[serial] = str(device_id)
        role = str(body.get("role") or body.get("device_type") or "test")
        devices[str(device_id)] = PrototypeDevice(
            device_id=str(device_id),
            device_type=str(body.get("device_type") or "unknown"),
            adb_serial=serial,
            voidfix_device_id=voidfix_id,
            enabled=bool(body.get("enabled")),
            role=role,
        )
    return devices


def _read_production_serials_readonly(slot_map_path: str | Path | None) -> frozenset[str]:
    if slot_map_path is None:
        return frozenset()
    path = Path(slot_map_path)
    if not path.exists():
        return frozenset()
    try:
        mapping = load_slot_map(path)
    except SlotMapError:
        return frozenset()
    return frozenset(str(serial) for serial in mapping.values())


def _reject_production_overlap(devices: dict[str, PrototypeDevice], production_serials: frozenset[str]) -> None:
    for device in devices.values():
        if device.adb_serial and device.adb_serial in production_serials:
            raise PrototypeConfigError(
                f"{device.device_id} uses a production slot_map serial; prototype devices must be separate"
            )
        if "farm" in device.role.lower() or "phonefarm" in device.role.lower():
            raise PrototypeConfigError(f"{device.device_id} role {device.role} is not allowed in prototype")


def _parse_mode(raw: str | None) -> PrototypeMode:
    if not raw or not raw.strip():
        return PrototypeMode.PRODUCTION_DISABLED
    try:
        return PrototypeMode(raw.strip().upper().replace("-", "_"))
    except ValueError as exc:
        raise PrototypeConfigError(f"unknown PROTOTYPE_MODE: {raw}") from exc


def _parse_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    items: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if token and token not in items and not token.upper().startswith(PLACEHOLDER_PREFIX):
            items.append(token)
    return tuple(items)


def _read_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise PrototypeConfigError(f"boolean value is invalid: {raw}")
