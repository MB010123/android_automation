"""Provisioning slot isolation, independent of slot_map.json.

``slot_map.json`` is ADB identity only. This policy is the sole source of
provisioning scope. Map size must never become the claim set.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


class SlotIsolationError(ValueError):
    """Raised when the claim set is empty or a slot is outside the allowlist."""


class SlotIsolationPolicy:
    """Hard allowlist for provisioning claim IDs.

    Default initial-testing allowlist is ``{1}``. The claim set is always
    ``mapped ∩ allowed``. An empty intersection refuses provisioning.
    """

    DEFAULT_ALLOWED_SLOT_IDS: frozenset[int] = frozenset({1})

    def __init__(self, allowed_slot_ids: Iterable[int] | None = None) -> None:
        if allowed_slot_ids is None:
            allowed = set(self.DEFAULT_ALLOWED_SLOT_IDS)
        else:
            allowed = {int(slot_id) for slot_id in allowed_slot_ids}
        for slot_id in allowed:
            if not 1 <= slot_id <= 20:
                raise SlotIsolationError(f"allowed slot_id must be 1-20, got {slot_id}")
        self._allowed = frozenset(allowed)

    @property
    def allowed_slot_ids(self) -> frozenset[int]:
        return self._allowed

    def claim_ids(self, mapped_slot_ids: Iterable[int] | Mapping[int, object]) -> list[int]:
        """Return sorted ``mapped ∩ allowed``. Never returns map keys alone."""
        mapped = set(_iter_slot_ids(mapped_slot_ids))
        return sorted(mapped & self._allowed)

    def refuse_if_empty(self, mapped_slot_ids: Iterable[int] | Mapping[int, object]) -> list[int]:
        ids = self.claim_ids(mapped_slot_ids)
        if not ids:
            raise SlotIsolationError(
                "empty provisioning claim set: refuse provisioning "
                "(mapped ∩ allowlist is empty)"
            )
        return ids

    def allows(self, slot_id: int) -> bool:
        return int(slot_id) in self._allowed

    def reject_if_outside(self, slot_id: int) -> None:
        if not self.allows(slot_id):
            raise SlotIsolationError(
                f"slot {slot_id} is outside the provisioning allowlist "
                f"{sorted(self._allowed)}"
            )

    def max_workers(
        self,
        configured: int,
        mapped_slot_ids: Iterable[int] | Mapping[int, object],
    ) -> int:
        """Bound worker count by the claim set, never by map size."""
        claim = self.refuse_if_empty(mapped_slot_ids)
        if configured < 1:
            raise SlotIsolationError("configured max_workers must be at least 1")
        return min(configured, len(claim))

    def claim_payload(self, mapped_slot_ids: Iterable[int] | Mapping[int, object]) -> dict[str, list[int]]:
        """JSON body for POST /claim. Raises if the intersection is empty."""
        return {"slot_ids": self.refuse_if_empty(mapped_slot_ids)}


def _iter_slot_ids(mapped_slot_ids: Iterable[int] | Mapping[int, object]) -> Iterable[int]:
    if isinstance(mapped_slot_ids, Mapping):
        return (int(slot_id) for slot_id in mapped_slot_ids.keys())
    return (int(slot_id) for slot_id in mapped_slot_ids)
