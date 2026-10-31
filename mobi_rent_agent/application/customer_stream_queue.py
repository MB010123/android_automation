"""4-stream customer live-session lease + FIFO wait queue."""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_CONCURRENT_CUSTOMER_STREAMS = int(os.getenv("MOBI_MAX_CONCURRENT_STREAMS", "4"))
STREAM_SLOT_TTL_SECONDS = int(os.getenv("MOBI_STREAM_SLOT_TTL_SECONDS", "480"))
STREAM_IDLE_HEARTBEAT_TIMEOUT = 45
SNAPSHOT_RELAXED_SECONDS = 10.0
_MIN_WAIT_SECONDS = 15


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class StreamLease:
    rental_id: str
    customer_id: str
    slot_id: int
    started_at: float
    last_seen_at: float
    expires_at: float
    stream_url: str | None = None
    prepared: bool = False
    needs_grant: bool = False


@dataclass
class QueueEntry:
    rental_id: str
    customer_id: str
    slot_id: int
    enqueued_at: float
    last_polled_at: float
    needs_grant: bool = True


@dataclass
class StreamDecision:
    queue_status: str
    lease: StreamLease | None = None
    queue_position: int | None = None
    estimated_wait_seconds: int | None = None
    estimated_wait_minutes: int | None = None
    message: str | None = None
    max_concurrent_streams: int = MAX_CONCURRENT_CUSTOMER_STREAMS
    tracked: bool = True


@dataclass
class CustomerStreamQueue:
    clock: Callable[[], float]
    max_concurrent: int = field(default_factory=lambda: _env_int("MOBI_MAX_CONCURRENT_STREAMS", 4))
    ttl_seconds: float = field(default_factory=lambda: float(_env_int("MOBI_STREAM_SLOT_TTL_SECONDS", 480)))
    idle_seconds: float = STREAM_IDLE_HEARTBEAT_TIMEOUT
    on_busy_change: Callable[[bool], None] | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self.active_leases: dict[str, StreamLease] = {}
        self.wait_queue: list[QueueEntry] = []
        self._admin_slots: set[int] = set()
        self._busy = False

    def admit(
        self,
        rental_id: str,
        customer_id: str,
        slot_id: int,
        *,
        claim: bool = False,
        admin: bool = False,
    ) -> StreamDecision:
        rental = str(rental_id or "").strip()
        customer = str(customer_id or "").strip()
        bay = int(slot_id)
        if admin:
            self.mark_admin(bay, True)
            return StreamDecision(queue_status="active", tracked=False)
        if not rental or not customer:
            return StreamDecision(queue_status="active", tracked=False)
        with self._lock:
            now = float(self.clock())
            self._prune_locked(now)
            lease = self.active_leases.get(rental)
            if lease is not None:
                lease.last_seen_at = now
                return self._active_decision(lease)
            queued = self._queued_entry(rental)
            if queued is not None:
                queued.last_polled_at = now
                queued.customer_id = customer or queued.customer_id
                queued.slot_id = bay or queued.slot_id
                if claim:
                    queued.needs_grant = True
                return self._queued_decision_locked(queued, now)
            if len(self.active_leases) < max(1, int(self.max_concurrent)):
                if not claim:
                    return StreamDecision(queue_status="active", tracked=False)
                lease = self._new_lease(rental, customer, bay, now, needs_grant=False)
                self.active_leases[rental] = lease
                self._publish_busy_locked()
                return self._active_decision(lease)
            entry = QueueEntry(
                rental_id=rental,
                customer_id=customer,
                slot_id=bay,
                enqueued_at=now,
                last_polled_at=now,
                needs_grant=True,
            )
            self.wait_queue.append(entry)
            return self._queued_decision_locked(entry, now)

    def mark_prepared(self, rental_id: str, stream_url: str | None = None) -> None:
        rental = str(rental_id or "").strip()
        with self._lock:
            lease = self.active_leases.get(rental)
            if lease is None:
                return
            lease.prepared = True
            lease.needs_grant = False
            if stream_url:
                lease.stream_url = stream_url
            lease.last_seen_at = float(self.clock())

    def release(self, rental_id: str) -> StreamLease | None:
        rental = str(rental_id or "").strip()
        with self._lock:
            now = float(self.clock())
            dropped = self.active_leases.pop(rental, None)
            self.wait_queue = [item for item in self.wait_queue if item.rental_id != rental]
            self._promote_locked(now)
            self._publish_busy_locked()
            return dropped

    def mark_admin(self, slot_id: int, active: bool) -> None:
        with self._lock:
            bay = int(slot_id)
            if active:
                self._admin_slots.add(bay)
            else:
                self._admin_slots.discard(bay)
            self._publish_busy_locked()

    def has_busy_streams(self) -> bool:
        with self._lock:
            self._prune_locked(float(self.clock()))
            return bool(self.active_leases or self._admin_slots)

    def peek(self, rental_id: str) -> StreamLease | None:
        rental = str(rental_id or "").strip()
        with self._lock:
            return self.active_leases.get(rental)

    def active_count(self) -> int:
        with self._lock:
            self._prune_locked(float(self.clock()))
            return len(self.active_leases)

    def queue_fields(self, decision: StreamDecision) -> dict[str, Any]:
        body: dict[str, Any] = {
            "queue_status": decision.queue_status,
            "max_concurrent_streams": int(decision.max_concurrent_streams),
        }
        if decision.queue_status == "active" and decision.lease is not None:
            now = float(self.clock())
            body["slot_remaining_seconds"] = max(0, int(decision.lease.expires_at - now))
            if decision.lease.stream_url:
                body["stream_url"] = decision.lease.stream_url
        if decision.queue_status == "queued":
            body.update(
                {
                    "status": "queued",
                    "queue_position": decision.queue_position,
                    "estimated_wait_seconds": decision.estimated_wait_seconds,
                    "estimated_wait_minutes": decision.estimated_wait_minutes,
                    "message": decision.message,
                    "stream_url": None,
                }
            )
        return body

    def queued_body(
        self,
        decision: StreamDecision,
        *,
        rental_id: str,
        slot_id: int,
    ) -> dict[str, Any]:
        body = {
            "ok": True,
            "rental_id": rental_id,
            "slot_id": int(slot_id),
            "active": False,
        }
        body.update(self.queue_fields(decision))
        return body

    def _new_lease(
        self,
        rental: str,
        customer: str,
        slot_id: int,
        now: float,
        *,
        needs_grant: bool,
    ) -> StreamLease:
        return StreamLease(
            rental_id=rental,
            customer_id=customer,
            slot_id=int(slot_id),
            started_at=now,
            last_seen_at=now,
            expires_at=now + float(self.ttl_seconds),
            stream_url=f"/rentals/{rental}/remote-access/stream",
            prepared=False,
            needs_grant=needs_grant,
        )

    def _queued_entry(self, rental: str) -> QueueEntry | None:
        for item in self.wait_queue:
            if item.rental_id == rental:
                return item
        return None

    def _active_decision(self, lease: StreamLease) -> StreamDecision:
        return StreamDecision(
            queue_status="active",
            lease=lease,
            max_concurrent_streams=int(self.max_concurrent),
        )

    def _queued_decision_locked(self, entry: QueueEntry, now: float) -> StreamDecision:
        position = 1
        for index, item in enumerate(self.wait_queue, start=1):
            if item.rental_id == entry.rental_id:
                position = index
                break
        remaining = sorted(
            max(0.0, lease.expires_at - now) for lease in self.active_leases.values()
        )
        if remaining:
            pick = remaining[min(position, len(remaining)) - 1]
        else:
            pick = float(self.ttl_seconds)
        seconds = max(_MIN_WAIT_SECONDS, min(int(self.ttl_seconds), int(math.ceil(pick))))
        minutes = max(1, int(math.ceil(seconds / 60.0)))
        return StreamDecision(
            queue_status="queued",
            queue_position=position,
            estimated_wait_seconds=seconds,
            estimated_wait_minutes=minutes,
            message=f"Up next in {minutes} minutes",
            max_concurrent_streams=int(self.max_concurrent),
        )

    def _prune_locked(self, now: float) -> None:
        expired = [
            rental
            for rental, lease in self.active_leases.items()
            if lease.expires_at <= now or (now - lease.last_seen_at) >= float(self.idle_seconds)
        ]
        for rental in expired:
            self.active_leases.pop(rental, None)
        self._promote_locked(now)

    def _promote_locked(self, now: float) -> None:
        while self.wait_queue and len(self.active_leases) < max(1, int(self.max_concurrent)):
            entry = self.wait_queue.pop(0)
            self.active_leases[entry.rental_id] = self._new_lease(
                entry.rental_id,
                entry.customer_id,
                entry.slot_id,
                now,
                needs_grant=entry.needs_grant,
            )
        self._publish_busy_locked()

    def _publish_busy_locked(self) -> None:
        busy = bool(self.active_leases or self._admin_slots)
        if busy == self._busy:
            return
        self._busy = busy
        callback = self.on_busy_change
        if callback is not None:
            callback(busy)
