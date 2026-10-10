"""Farm-side operator inventory JOIN for slots 1–20.

Customer-visible identity is ``public.slots`` (imei2, carrier_name, phone_number)
plus rental/slot ``eid``. This module never writes those columns. It joins Farm
operator files by ``slot_id`` only and leaves missing fields unknown.

Farm ``slot_map`` serials stay Farm-only. They are never copied into a
customer-facing payload. Values are never invented, never copied from Slot 1
onto other bays, and never derived (IMEI from serial, EID from IMEI, carrier
from Wi-Fi/ADB, phone from IMEI/EID).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from application.vps_api_contract import customer_inventory_identity_fields
from domain.farm import EXPECTED_PRODUCTION_SLOT_COUNT, EXPECTED_PRODUCTION_SLOT_IDS
from domain.models import SlotDeviceRecord

CUSTOMER_FORBIDDEN_KEYS = frozenset(
    {
        "serial",
        "adb_serial",
        "device_serial",
        "udid",
        "voidfix_device_id",
        "imei1",
        "farm_sms_phone",
        "farm_sms_msisdn",
        "slot_map",
    }
)

SOURCE_DEVICE_REGISTRY = "device_registry"
SOURCE_TENANT_SLOTS = "public.slots"
SOURCE_FARM_SMS_MAP = "farm_slot_msisdn_map"
SOURCE_LIVE_ADB_IMEI = "cmd phone get-imei"
SOURCE_COMPANION_IMEI = "companion get_imei_access"

REQUIRED_FOR_UNKNOWN_IMEI = (
    "Privileged IMEI read (READ_PRIVILEGED_PHONE_STATE / operator label) "
    "or a manual device_registry.json entry for this slot_id. "
    "Unprivileged ADB `cmd phone get-imei` is expected to be Permission denied "
    "on Android 10+. Farm Agent has no get_imei task."
)
REQUIRED_FOR_UNKNOWN_EID = (
    "Manual operator inventory of the eUICC EID into public.slots/rental eid. "
    "No Farm get_eid task, no device_registry EID field, no SLOT_SAFE_FIELDS EID "
    "column, and live EuiccManager / dumpsys EID scraping are not allowlisted."
)
REQUIRED_FOR_UNKNOWN_CARRIER = (
    "Operator/admin sets public.slots.carrier_name via the existing Lovable/admin "
    "path. Do not infer carrier from Wi-Fi, ADB, or companion networkOperatorName."
)
REQUIRED_FOR_UNKNOWN_CUSTOMER_PHONE = (
    "Operator/admin sets public.slots.phone_number via the existing Lovable/admin "
    "path after confirming the customer-visible number. Farm slot_msisdn_map.json "
    "is SMS routing only and must not be blindly copied."
)


@dataclass(frozen=True)
class LiveImeiObservation:
    imei1: str | None = None
    imei2: str | None = None
    imei1_accessible: bool = False
    imei2_accessible: bool = False
    source: str = SOURCE_LIVE_ADB_IMEI
    error: str | None = None

    @property
    def result(self) -> str:
        if self.imei1_accessible or self.imei2_accessible:
            return "works"
        err = (self.error or "").lower()
        if "permission denied" in err or "securityexception" in err:
            return "permission_denied"
        if err:
            return "unknown"
        return "unknown"


@dataclass(frozen=True)
class CompanionObservation:
    command: str = "get_imei_access"
    ok: bool = False
    unknown_command: bool = False
    imei1: str | None = None
    imei2: str | None = None
    imei1_accessible: bool = False
    imei2_accessible: bool = False
    error: str | None = None
    source: str = SOURCE_COMPANION_IMEI

    @property
    def result(self) -> str:
        if self.unknown_command:
            return "apk_missing_command"
        if self.imei1_accessible or self.imei2_accessible:
            return "works"
        err = (self.error or "").lower()
        if "permission denied" in err or "securityexception" in err:
            return "permission_denied"
        if self.ok and not self.imei1_accessible and not self.imei2_accessible:
            return "permission_denied"
        if err:
            return "unknown"
        return "unknown"


@dataclass(frozen=True)
class PreparedSlotInventory:
    slot_id: int
    serial_mapped: bool
    serial: str | None
    imei1: str | None
    imei1_source: str | None
    imei2: str | None
    imei2_source: str | None
    eid: str | None
    eid_source: str | None
    carrier: str | None
    carrier_source: str | None
    farm_sms_phone: str | None
    tenant_phone: str | None
    tenant_imei2: str | None
    tenant_carrier: str | None
    tenant_eid: str | None
    live_imei: LiveImeiObservation | None = None
    companion: CompanionObservation | None = None
    adb_state: str | None = None

    def customer_payload(self) -> dict[str, Any]:
        """Current customer-visible inventory: public.slots / tenant row only."""
        payload = customer_inventory_identity_fields(
            imei2=self.tenant_imei2,
            eid=self.tenant_eid,
            carrier=self.tenant_carrier,
            phone_number=self.tenant_phone,
        )
        forbidden = CUSTOMER_FORBIDDEN_KEYS.intersection(payload)
        if forbidden:
            raise RuntimeError(f"customer payload leaked forbidden keys: {sorted(forbidden)}")
        if self.serial and self.serial in str(payload):
            raise RuntimeError("customer payload leaked ADB serial")
        return payload

    def recommended_public_slots(self) -> dict[str, str | None]:
        """Fields an operator may later write to public.slots. Never serials.

        IMEI2 may come from the Farm registry or a successful approved live
        read for this slot_id only. Carrier, EID, and customer phone stay
        tenant-only unless already stored — Farm SMS numbers are not copied.
        """
        imei2 = self.tenant_imei2 or self.imei2
        return {
            "motherboard_slot_num": str(self.slot_id),
            "imei2": imei2,
            "carrier_name": self.tenant_carrier,
            "phone_number": self.tenant_phone,
            "eid": self.tenant_eid,
        }

    def manual_operator_fields(self) -> list[str]:
        recommended = self.recommended_public_slots()
        missing: list[str] = []
        if not recommended["imei2"]:
            missing.append("imei2")
        if not recommended["carrier_name"]:
            missing.append("carrier_name")
        if not recommended["phone_number"]:
            missing.append("phone_number")
        if not recommended["eid"]:
            missing.append("eid")
        return missing

    def operator_row(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "serial_mapped": self.serial_mapped,
            "serial_last4": _last4(self.serial) if self.serial_mapped else None,
            "adb_state": self.adb_state,
            "imei1": _field(self.imei1, self.imei1_source, kind="imei"),
            "imei2": _field(self.imei2, self.imei2_source, kind="imei"),
            "eid": _field(self.eid, self.eid_source, kind="eid"),
            "carrier": _field(self.carrier, self.carrier_source, kind="text"),
            "phone_number_farm_sms": _field(
                self.farm_sms_phone, SOURCE_FARM_SMS_MAP if self.farm_sms_phone else None, kind="phone"
            ),
            "phone_number_customer": _field(
                self.tenant_phone, SOURCE_TENANT_SLOTS if self.tenant_phone else None, kind="phone"
            ),
            "live_imei_probe": _live_imei_row(self.live_imei),
            "companion_get_imei_access": _companion_row(self.companion),
            "customer_payload": self.customer_payload(),
            "recommended_public_slots": _redact_recommended(self.recommended_public_slots()),
            "manual_operator_fields": self.manual_operator_fields(),
            "required_if_unknown": {
                "imei": REQUIRED_FOR_UNKNOWN_IMEI if not self.imei2 else None,
                "eid": REQUIRED_FOR_UNKNOWN_EID if not self.eid else None,
                "carrier": REQUIRED_FOR_UNKNOWN_CARRIER if not self.carrier else None,
                "customer_phone": REQUIRED_FOR_UNKNOWN_CUSTOMER_PHONE if not self.tenant_phone else None,
            },
        }


@dataclass(frozen=True)
class PreparedDeviceInventory:
    slots: tuple[PreparedSlotInventory, ...]
    farm_agent_health: dict[str, Any] | None = None
    notes: tuple[str, ...] = ()

    def to_operator_report(self) -> dict[str, Any]:
        return {
            "kind": "mobi_rent_operator_device_inventory_prepare",
            "customer_source_of_truth": (
                "public.slots (imei2, carrier_name, phone_number) + rental/slot eid; "
                "customer device-status already passthroughs owned rows"
            ),
            "farm_operator_sources": {
                "slot_map": "Farm-only ADB serial mapping; never customer-facing",
                "device_registry": "Farm operator IMEI1/IMEI2 cache; keyed by slot_id",
                "slot_msisdn_map": "Farm SMS routing; not customer inventory",
            },
            "production_write": False,
            "slot_count": len(self.slots),
            "expected_slot_count": EXPECTED_PRODUCTION_SLOT_COUNT,
            "farm_agent_health": self.farm_agent_health,
            "notes": list(self.notes),
            "recommended_sync": (
                "Operator/admin updates public.slots.imei2 / carrier_name / "
                "phone_number / eid via the existing Lovable/admin path using "
                "recommended_public_slots per bay. Do not copy slot_map serials. "
                "Do not blindly copy Farm slot_msisdn_map into public.slots.phone_number. "
                "Do not POST Windows JSON onto VPS public.slots from this report. "
                "VPS device-status already passthroughs owned public.slots."
            ),
            "slots": [slot.operator_row() for slot in self.slots],
        }


def prepare_device_inventory(
    *,
    slot_map: Mapping[int, str],
    device_registry: Mapping[int, SlotDeviceRecord | Mapping[str, Any]] | None = None,
    farm_msisdn_map: Mapping[int, str] | None = None,
    tenant_slots: Mapping[int, Mapping[str, Any]] | None = None,
    live_imei: Mapping[int, LiveImeiObservation] | None = None,
    companion: Mapping[int, CompanionObservation] | None = None,
    adb_states: Mapping[str, str] | None = None,
    farm_agent_health: dict[str, Any] | None = None,
    notes: tuple[str, ...] | list[str] | None = None,
) -> PreparedDeviceInventory:
    registry = _index_registry(device_registry or {})
    msisdn = {int(slot_id): _text(value) for slot_id, value in (farm_msisdn_map or {}).items()}
    tenants = {int(slot_id): dict(row) for slot_id, row in (tenant_slots or {}).items() if isinstance(row, Mapping)}
    live = {int(slot_id): obs for slot_id, obs in (live_imei or {}).items()}
    comps = {int(slot_id): obs for slot_id, obs in (companion or {}).items()}
    mapped = {int(slot_id): str(serial).strip() for slot_id, serial in slot_map.items() if str(serial).strip()}

    slots: list[PreparedSlotInventory] = []
    for slot_id in sorted(EXPECTED_PRODUCTION_SLOT_IDS):
        serial = mapped.get(slot_id)
        record = registry.get(slot_id)
        tenant = tenants.get(slot_id) or {}
        live_obs = live.get(slot_id)
        comp_obs = comps.get(slot_id)
        tenant_imei2 = _text(tenant.get("imei2"))
        tenant_carrier = _text(tenant.get("carrier_name") or tenant.get("carrier"))
        tenant_phone = _text(tenant.get("phone_number"))
        tenant_eid = _text(tenant.get("eid"))
        registry_imei1 = _text(getattr(record, "imei1", None) if record is not None else None)
        registry_imei2 = _text(getattr(record, "imei2", None) if record is not None else None)
        live_imei1 = _text(live_obs.imei1) if live_obs and live_obs.imei1_accessible else None
        live_imei2 = _text(live_obs.imei2) if live_obs and live_obs.imei2_accessible else None
        comp_imei1 = _text(comp_obs.imei1) if comp_obs and comp_obs.imei1_accessible else None
        comp_imei2 = _text(comp_obs.imei2) if comp_obs and comp_obs.imei2_accessible else None

        imei1, imei1_source = _first_known(
            (registry_imei1, SOURCE_DEVICE_REGISTRY),
            (comp_imei1, SOURCE_COMPANION_IMEI),
            (live_imei1, SOURCE_LIVE_ADB_IMEI),
        )
        imei2, imei2_source = _first_known(
            (tenant_imei2, SOURCE_TENANT_SLOTS),
            (registry_imei2, SOURCE_DEVICE_REGISTRY),
            (comp_imei2, SOURCE_COMPANION_IMEI),
            (live_imei2, SOURCE_LIVE_ADB_IMEI),
        )
        eid, eid_source = _first_known((tenant_eid, SOURCE_TENANT_SLOTS))
        carrier, carrier_source = _first_known((tenant_carrier, SOURCE_TENANT_SLOTS))
        adb_state = None
        if serial and adb_states is not None:
            adb_state = adb_states.get(serial) or "missing"

        slots.append(
            PreparedSlotInventory(
                slot_id=slot_id,
                serial_mapped=serial is not None,
                serial=serial,
                imei1=imei1,
                imei1_source=imei1_source,
                imei2=imei2,
                imei2_source=imei2_source,
                eid=eid,
                eid_source=eid_source,
                carrier=carrier,
                carrier_source=carrier_source,
                farm_sms_phone=msisdn.get(slot_id),
                tenant_phone=tenant_phone,
                tenant_imei2=tenant_imei2,
                tenant_carrier=tenant_carrier,
                tenant_eid=tenant_eid,
                live_imei=live_obs,
                companion=comp_obs,
                adb_state=adb_state,
            )
        )

    default_notes = (
        "Customer API reads public.slots; this report does not write production.",
        "Farm slot_map serials are omitted from customer_payload.",
        "Farm slot_msisdn_map is SMS routing, distinct from public.slots.phone_number.",
        "VPS public.slots / VPS slot_msisdn_map were not read (no deploy / no SSH).",
    )
    return PreparedDeviceInventory(
        slots=tuple(slots),
        farm_agent_health=farm_agent_health,
        notes=tuple(notes) if notes is not None else default_notes,
    )


def observe_live_imei(serial: str, reader: Any) -> LiveImeiObservation:
    """READ-ONLY wrapper around existing AdbImeiReader. Does not add ADB commands."""
    result = reader.read(serial)
    return LiveImeiObservation(
        imei1=_text(getattr(result, "imei1", None)),
        imei2=_text(getattr(result, "imei2", None)),
        imei1_accessible=bool(getattr(result, "imei1_accessible", False)),
        imei2_accessible=bool(getattr(result, "imei2_accessible", False)),
        source=str(getattr(result, "source", None) or SOURCE_LIVE_ADB_IMEI),
        error=_text(getattr(result, "error", None)),
    )


def observe_companion_imei_access(serial: str, client: Any) -> CompanionObservation:
    """READ-ONLY companion get_imei_access using the existing forwarded JSON client."""
    try:
        payload = client.request(serial, {"command": "get_imei_access"})
    except Exception as exc:  # noqa: BLE001 - probe must stay unknown, never fabricated
        return CompanionObservation(ok=False, error=exc.__class__.__name__)
    if not isinstance(payload, dict):
        return CompanionObservation(ok=False, error="invalid_companion_response")
    error = _text(payload.get("error"))
    unknown = bool(error and "unknown command" in error.lower())
    imei1 = _text(payload.get("imei1"))
    imei2 = _text(payload.get("imei2"))
    imei1_accessible = bool(payload.get("imei1_accessible")) or bool(imei1)
    imei2_accessible = bool(payload.get("imei2_accessible")) or bool(imei2)
    return CompanionObservation(
        ok=bool(payload.get("success")),
        unknown_command=unknown,
        imei1=imei1,
        imei2=imei2,
        imei1_accessible=imei1_accessible,
        imei2_accessible=imei2_accessible,
        error=error,
    )


def mask_imei_last4(value: str | None) -> str | None:
    text = _text(value)
    if not text:
        return None
    return f"****{text[-4:]}" if len(text) >= 4 else "****"


def mask_phone_last4(value: str | None) -> str | None:
    text = _text(value)
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 4:
        return "+***"
    return f"+***{digits[-4:]}"


def _last4(value: str | None) -> str | None:
    text = _text(value)
    if not text:
        return None
    return text[-4:] if len(text) >= 4 else text


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_known(*pairs: tuple[str | None, str | None]) -> tuple[str | None, str | None]:
    for value, source in pairs:
        if value:
            return value, source
    return None, None


def _index_registry(
    device_registry: Mapping[int, SlotDeviceRecord | Mapping[str, Any]],
) -> dict[int, SlotDeviceRecord]:
    indexed: dict[int, SlotDeviceRecord] = {}
    for key, body in device_registry.items():
        slot_id = int(key)
        if isinstance(body, SlotDeviceRecord):
            if body.slot_id != slot_id:
                raise ValueError(f"device_registry slot {slot_id} record.slot_id is {body.slot_id}")
            indexed[slot_id] = body
            continue
        if not isinstance(body, Mapping):
            raise ValueError(f"device_registry slot {slot_id} must be an object")
        if body.get("adb_serial"):
            raise ValueError("device registry must not store adb_serial; use slot_map.json")
        imei2 = _text(body.get("imei2"))
        if not imei2:
            continue
        indexed[slot_id] = SlotDeviceRecord(
            slot_id=slot_id,
            imei2=imei2,
            imei1=_text(body.get("imei1")),
        )
    return indexed


def _field(value: str | None, source: str | None, *, kind: str) -> dict[str, Any]:
    if not value:
        return {"value_redacted": None, "status": "unknown", "source": None}
    if kind == "imei":
        redacted = mask_imei_last4(value)
    elif kind == "phone":
        redacted = mask_phone_last4(value)
    elif kind == "eid":
        redacted = f"****{value[-4:]}" if len(value) >= 4 else "****"
    else:
        redacted = value
    return {"value_redacted": redacted, "status": "known", "source": source}


def _redact_recommended(row: dict[str, str | None]) -> dict[str, str | None]:
    return {
        "motherboard_slot_num": row["motherboard_slot_num"],
        "imei2": mask_imei_last4(row.get("imei2")),
        "carrier_name": row.get("carrier_name"),
        "phone_number": mask_phone_last4(row.get("phone_number")),
        "eid": f"****{row['eid'][-4:]}" if row.get("eid") and len(row["eid"]) >= 4 else row.get("eid"),
    }


def _live_imei_row(obs: LiveImeiObservation | None) -> dict[str, Any] | None:
    if obs is None:
        return None
    return {
        "result": obs.result,
        "source": obs.source,
        "imei1_accessible": obs.imei1_accessible,
        "imei2_accessible": obs.imei2_accessible,
        "imei1_redacted": mask_imei_last4(obs.imei1) if obs.imei1_accessible else None,
        "imei2_redacted": mask_imei_last4(obs.imei2) if obs.imei2_accessible else None,
        "error": obs.error,
    }


def _companion_row(obs: CompanionObservation | None) -> dict[str, Any] | None:
    if obs is None:
        return None
    return {
        "result": obs.result,
        "command": obs.command,
        "ok": obs.ok,
        "unknown_command": obs.unknown_command,
        "imei1_accessible": obs.imei1_accessible,
        "imei2_accessible": obs.imei2_accessible,
        "error": obs.error,
    }
