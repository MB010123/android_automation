"""Verify that one requested farm slot maps to exactly one live device."""
from __future__ import annotations

from collections.abc import Callable, Mapping

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
