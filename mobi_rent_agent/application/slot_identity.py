"""Verify that one requested farm slot maps to exactly one live device."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from domain.farm import FarmSlotState, ISOLATED_PROTOTYPE_SERIALS
from infrastructure.redact import redact_serial


class SlotIdentityError(ValueError):
    def __init__(self, message: str, state: FarmSlotState = FarmSlotState.MAPPING_ERROR) -> None:
        super().__init__(message)
        self.state = state


class SlotIdentityGuard:
    """Rejects ambiguous, missing, duplicate, or changed slot identity."""

    def __init__(
        self,
        slot_map: Mapping[int, str],
        *,
        adb_states: Mapping[str, str] | None = None,
        list_states: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._slot_map = {int(slot_id): str(serial).strip() for slot_id, serial in slot_map.items()}
        self._adb_states = dict(adb_states) if adb_states is not None else None
        self._list_states = list_states
        self._serial_to_slot: dict[str, int] = {}
        for slot_id, serial in self._slot_map.items():
            previous = self._serial_to_slot.get(serial)
            if previous is not None and previous != slot_id:
                raise SlotIdentityError(
                    f"serial assigned to multiple slots ({previous} and {slot_id})",
                    FarmSlotState.MAPPING_ERROR,
                )
            if serial:
                self._serial_to_slot[serial] = slot_id

    def expected_serial(self, slot_id: int) -> str:
        serial = self._slot_map.get(int(slot_id))
        if not serial:
            raise SlotIdentityError(
                f"slot {slot_id} has no unique device mapping",
                FarmSlotState.MAPPING_ERROR,
            )
        owner = self._serial_to_slot.get(serial)
        if owner != int(slot_id):
            raise SlotIdentityError(
                f"slot {slot_id} serial is not uniquely owned",
                FarmSlotState.MAPPING_ERROR,
            )
        if serial in ISOLATED_PROTOTYPE_SERIALS:
            raise SlotIdentityError(
                f"slot {slot_id} points at the isolated prototype device",
                FarmSlotState.MAPPING_ERROR,
            )
        return serial

    def verify(self, slot_id: int) -> str:
        """Return the expected serial or raise. Live ADB is checked when available."""
        serial = self.expected_serial(slot_id)
        states = self._current_states()
        if states is None:
            return serial
        state = states.get(serial) or "missing"
        if state == "unauthorized":
            raise SlotIdentityError(
                f"slot {slot_id} device is unauthorized ({redact_serial(serial)})",
                FarmSlotState.DISCONNECTED,
            )
        if state != "device":
            raise SlotIdentityError(
                f"slot {slot_id} device is {state} ({redact_serial(serial)})",
                FarmSlotState.DISCONNECTED,
            )
        return serial

    def recheck_after_reconnect(self, slot_id: int) -> str:
        """Identity must still match the mapped serial after ADB returns."""
        return self.verify(slot_id)

    def _current_states(self) -> Mapping[str, str] | None:
        if self._list_states is not None:
            return self._list_states()
        return self._adb_states


def serial_for_slot(slot_map: Mapping[int, str] | None, bay: int) -> str | None:
    if not slot_map:
        return None
    try:
        slot = int(bay)
    except (TypeError, ValueError):
        return None
    serial = str(slot_map.get(slot) or "").strip()
    return serial or None


def farm_slot_entries(
    slot_map: Mapping[int, str],
    *,
    offline_slots: set[int] | None = None,
    adb_states: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Per-bay identity list for Farm health / GET /farm/status."""
    offline = {int(s) for s in (offline_slots or set())}
    slots: list[dict[str, Any]] = []
    for bay, serial in sorted((int(k), str(v).strip()) for k, v in slot_map.items()):
        if not serial:
            continue
        if adb_states is not None:
            state = str(adb_states.get(serial) or "missing")
        elif bay in offline:
            state = "offline"
        else:
            state = "device"
        slots.append(
            {
                "slot_id": bay,
                "serial": serial,
                "adb_state": state,
                "online": state == "device",
            }
        )
    return slots


def serial_from_farm_slots(farm: Mapping[str, Any] | None, bay: int) -> str | None:
    """Read a mapped serial from Farm health ``slots[]`` without a local slot_map."""
    if not isinstance(farm, Mapping):
        return None
    try:
        wanted = int(bay)
    except (TypeError, ValueError):
        return None
    raw = farm.get("slots")
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            slot_id = int(item.get("slot_id"))
        except (TypeError, ValueError):
            continue
        if slot_id != wanted:
            continue
        serial = str(item.get("serial") or "").strip()
        if serial:
            return serial
    return None


def overlay_farm_slot_identity(
    farm: dict[str, Any],
    slot_map: Mapping[int, str] | None,
) -> dict[str, Any]:
    """Ensure Farm health JSON includes integer slot_id + serial per mapped bay."""
    body = dict(farm)
    existing = body.get("slots")
    if isinstance(existing, list) and existing:
        return body
    mapped: dict[int, str] = {}
    for key, value in (slot_map or {}).items():
        try:
            mapped[int(key)] = str(value).strip()
        except (TypeError, ValueError):
            continue
    if not mapped:
        raw = body.get("mapped_slots") or []
        if isinstance(raw, list):
            for item in raw:
                try:
                    mapped[int(item)] = ""
                except (TypeError, ValueError):
                    continue
    offline_raw = body.get("offline_slots") or []
    offline: set[int] = set()
    if isinstance(offline_raw, list):
        for item in offline_raw:
            try:
                offline.add(int(item))
            except (TypeError, ValueError):
                continue
    body["slots"] = farm_slot_entries(mapped, offline_slots=offline)
    return body
