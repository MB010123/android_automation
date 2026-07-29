"""Unit tests for HeartbeatService using in-memory fakes for every port.

No network, no ADB, no real sleeping - the whole application layer is
tested in isolation, which is the point of keeping ports/adapters
separate from the use case.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.heartbeat_service import HeartbeatService, HeartbeatServiceConfig
from application.retry_policy import RetryPolicy
from domain.models import HeartbeatPayload, HeartbeatResult, SlotState, SlotStatus
from domain.ports import Clock, HeartbeatTransport, SlotStatusProvider


class FakeTransport(HeartbeatTransport):
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.sent_payloads: list[HeartbeatPayload] = []

    def send(self, payload: HeartbeatPayload) -> HeartbeatResult:
        self.sent_payloads.append(payload)
        if self.should_fail:
            return HeartbeatResult(success=False, slot_id=payload.slot_id, error="simulated failure")
        return HeartbeatResult(success=True, slot_id=payload.slot_id, status_code=200)


class FakeSlotStatusProvider(SlotStatusProvider):
    def __init__(self, states: list[SlotState]) -> None:
        self._states = states

    def read_slot_states(self):
        return list(self._states)


class FakeClock(Clock):
    def __init__(self) -> None:
        self.slept_for: list[float] = []
        self._t = 0.0

    def sleep(self, seconds: float) -> None:
        self.slept_for.append(seconds)
        self._t += seconds

    def monotonic(self) -> float:
        return self._t


def make_service(transport: FakeTransport, states: list[SlotState]) -> HeartbeatService:
    return HeartbeatService(
        config=HeartbeatServiceConfig(hardware_agent_token="tok123", interval_seconds=15.0),
        transport=transport,
        slot_status_provider=FakeSlotStatusProvider(states),
        clock=FakeClock(),
        retry_policy=RetryPolicy(base_delay_seconds=2.0, max_delay_seconds=8.0, multiplier=2.0),
    )


def test_run_once_sends_one_heartbeat_per_slot():
    transport = FakeTransport()
    states = [
        SlotState(1, SlotStatus.ONLINE),
        SlotState(2, SlotStatus.NETWORK_ERROR),
    ]
    service = make_service(transport, states)

    results = service.run_once()

    assert len(results) == 2
    assert all(r.success for r in results)
    assert [p.slot_id for p in transport.sent_payloads] == [1, 2]
    assert transport.sent_payloads[0].hardware_agent_token == "tok123"
    assert transport.sent_payloads[1].status == SlotStatus.NETWORK_ERROR


def test_run_once_reports_failure_without_raising():
    transport = FakeTransport(should_fail=True)
    states = [SlotState(1, SlotStatus.ONLINE)]
    service = make_service(transport, states)

    results = service.run_once()

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].error == "simulated failure"


def test_backoff_increases_after_consecutive_failures_and_resets_on_success():
    transport = FakeTransport(should_fail=True)
    states = [SlotState(1, SlotStatus.ONLINE)]
    service = make_service(transport, states)

    service.run_once()
    first_backoff = service._current_interval()
    service.run_once()
    second_backoff = service._current_interval()

    assert second_backoff > first_backoff

    transport.should_fail = False
    service.run_once()
    assert service._current_interval() == 15.0


def test_provider_exception_does_not_crash_run_once():
    class RaisingProvider(SlotStatusProvider):
        def read_slot_states(self):
            raise RuntimeError("adb exploded")

    service = HeartbeatService(
        config=HeartbeatServiceConfig(hardware_agent_token="tok123"),
        transport=FakeTransport(),
        slot_status_provider=RaisingProvider(),
        clock=FakeClock(),
    )

    results = service.run_once()
    assert results == []
