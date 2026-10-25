"""Preferred-first farm bay ordering for the next rental assignment."""
from __future__ import annotations

from collections.abc import Iterable

from domain.farm import MAX_FARM_SLOT_ID

DEFAULT_PREFERRED_NEXT_SLOT_IDS = (21,)


def parse_preferred_next_slot_ids(raw: str | None) -> tuple[int, ...]:
    """Parse ``PREFERRED_NEXT_SLOT_IDS`` CSV. Unset/blank defaults to Bay 21."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PREFERRED_NEXT_SLOT_IDS
    out: list[int] = []
    seen: set[int] = set()
    for part in str(raw).split(","):
        token = part.strip()
        if not token:
            continue
        try:
            slot = int(token)
        except ValueError:
            continue
        if 1 <= slot <= MAX_FARM_SLOT_ID and slot not in seen:
            seen.add(slot)
            out.append(slot)
    return tuple(out) or DEFAULT_PREFERRED_NEXT_SLOT_IDS


def sort_available_bays(
    bays: Iterable[int],
    preferred: tuple[int, ...] | None = None,
) -> list[int]:
    """Return preferred bays first (in config order), then remaining bays sorted."""
    available = {int(bay) for bay in bays}
    order = preferred if preferred is not None else DEFAULT_PREFERRED_NEXT_SLOT_IDS
    first = [bay for bay in order if bay in available]
    rest = sorted(bay for bay in available if bay not in set(order))
    return first + rest
