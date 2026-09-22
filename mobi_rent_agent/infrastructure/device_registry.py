"""Slot device identity registry (IMEI / IMEI2), separate from ADB serials.

slot_map.json remains bay -> ADB serial only. This registry maps bay ->
IMEI1 / IMEI2. IMEI2 is the Pixel eSIM digital IMEI for US Mobile.

The live file is gitignored. Never log raw IMEI values.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from domain.models import SlotDeviceRecord
from infrastructure.imei import is_valid_imei, redact_imei

logger = logging.getLogger("mobi_rent_agent.device_registry")

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "device_registry.json"


class DeviceRegistryError(RuntimeError):
    """The device registry file is missing or invalid."""


def _validate_imei(label: str, value: str | None, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            raise DeviceRegistryError(f"{label} is required")
        return None
    if not is_valid_imei(value):
        raise DeviceRegistryError(f"{label} is not a valid 15-digit IMEI")
    return value


def load_device_registry(path: str | Path | None = None) -> dict[int, SlotDeviceRecord]:
    path = Path(path) if path else DEFAULT_REGISTRY_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DeviceRegistryError(f"Invalid JSON in device registry {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DeviceRegistryError("device registry must be a JSON object keyed by slot_id")

    records: dict[int, SlotDeviceRecord] = {}
    for key, body in raw.items():
        try:
            slot_id = int(key)
        except (TypeError, ValueError) as exc:
            raise DeviceRegistryError("device registry keys must be slot ids 1-20") from exc
        if not isinstance(body, dict):
            raise DeviceRegistryError(f"slot {slot_id} entry must be an object")
        imei2 = _validate_imei("imei2", body.get("imei2"), required=True)
        imei1 = _validate_imei("imei1", body.get("imei1"), required=False)
        adb_serial = body.get("adb_serial")
        if adb_serial:
            raise DeviceRegistryError("device registry must not store adb_serial; use slot_map.json")
        record = SlotDeviceRecord(slot_id=slot_id, imei2=imei2 or "", imei1=imei1)
        records[slot_id] = record
    logger.info("Loaded device registry slots=%s", sorted(records))
    return records


def save_device_registry(records: dict[int, SlotDeviceRecord], path: str | Path | None = None) -> None:
    path = Path(path) if path else DEFAULT_REGISTRY_PATH
    payload = {str(slot_id): record.to_registry_dict() for slot_id, record in sorted(records.items())}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote device registry slots=%s path=%s", sorted(records), path.name)


def upsert_slot_record(
    record: SlotDeviceRecord,
    path: str | Path | None = None,
    adb_serial: str | None = None,
) -> SlotDeviceRecord:
    if adb_serial and record.imei2 == adb_serial:
        raise DeviceRegistryError("imei2 must not equal the ADB serial")
    if adb_serial and record.imei1 == adb_serial:
        raise DeviceRegistryError("imei1 must not equal the ADB serial")
    _validate_imei("imei2", record.imei2, required=True)
    _validate_imei("imei1", record.imei1, required=False)
    records = load_device_registry(path)
    records[record.slot_id] = record
    save_device_registry(records, path)
    logger.info(
        "Registered slot %s imei2=%s imei1=%s",
        record.slot_id,
        redact_imei(record.imei2),
        redact_imei(record.imei1) if record.imei1 else "<missing>",
    )
    return record


def imei2_for_slot(slot_id: int, path: str | Path | None = None) -> str | None:
    """Return the digital/eSIM IMEI for a bay. For backend/frontend lookup by slot_id."""
    records = load_device_registry(path)
    record = records.get(slot_id)
    return record.imei2 if record else None
