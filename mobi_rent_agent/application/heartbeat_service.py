"""Application use case: the heartbeat loop.

This orchestrates domain + ports only. It has zero knowledge of HTTP,
ADB, config files, or logging frameworks - those are injected as
`HeartbeatTransport` / `SlotStatusProvider` / `Clock` implementations.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from domain.models import HeartbeatPayload, HeartbeatResult, SlotState
from domain.ports import Clock, HeartbeatTransport, SlotStatusProvider
from application.retry_policy import RetryPolicy

logger = logging.getLogger("mobi_rent_agent.heartbeat")


@dataclass
class HeartbeatServiceConfig:
    hardware_agent_token: str
    interval_seconds: float = 15.0


class HeartbeatService:
    """Runs the "check hardware identifier -> ping backend" loop described
    in Phase 1:

        - runs on boot
        - checks its hardware identifier / per-slot status
        - securely pings the queue endpoint to update the central DB
        - retries gracefully on connection drop-outs without crashing
    """

    def __init__(
        self,
        config: HeartbeatServiceConfig,
        transport: HeartbeatTransport,
        slot_status_provider: SlotStatusProvider,
        clock: Clock,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._slot_status_provider = slot_status_provider
        self._clock = clock
        self._retry_policy = retry_policy or RetryPolicy()
        self._consecutive_failures = 0
        self._running = False

    def run_forever(self) -> None:
        """Blocking loop. Intended to be the daemon's main loop, run under
        systemd/supervisor so process-level crashes are also auto-restarted
        as a second line of defense."""
        self._running = True
        logger.info("Heartbeat service starting (interval=%.1fs)", self._config.interval_seconds)
        while self._running:
            self.run_once()
            self._clock.sleep(self._current_interval())

    def stop(self) -> None:
        self._running = False

    def run_once(self) -> list[HeartbeatResult]:
        """Read every known slot's state and send one heartbeat per slot.

        Never raises: all transport/provider failures are caught, logged,
        and folded into the backoff counter so the daemon keeps running.
        """
        results: list[HeartbeatResult] = []
        try:
            slot_states = list(self._slot_status_provider.read_slot_states())
        except Exception as exc:  # provider must never crash the daemon
            logger.exception("Failed to read slot states: %s", exc)
            self._register_failure()
            return results

        if not slot_states:
            logger.warning("No slots reported by SlotStatusProvider")

        any_failure = False
        for slot_state in slot_states:
            result = self._send_heartbeat_for_slot(slot_state)
            results.append(result)
            if not result.success:
                any_failure = True

        if any_failure:
            self._register_failure()
        else:
            self._register_success()

        return results

    def _send_heartbeat_for_slot(self, slot_state: SlotState) -> HeartbeatResult:
        payload = HeartbeatPayload(
            hardware_agent_token=self._config.hardware_agent_token,
            slot_id=slot_state.slot_id,
            status=slot_state.status,
        )
        try:
            result = self._transport.send(payload)
        except Exception as exc:  # transport should not raise, but be defensive
            logger.exception("Unexpected transport error for slot %s: %s", slot_state.slot_id, exc)
            return HeartbeatResult(success=False, slot_id=slot_state.slot_id, error=str(exc))

        if result.success:
            logger.debug("Heartbeat OK slot=%s status=%s", slot_state.slot_id, slot_state.status.value)
        else:
            logger.warning(
                "Heartbeat failed slot=%s status_code=%s error=%s",
                slot_state.slot_id,
                result.status_code,
                result.error,
            )
        return result

    def _register_failure(self) -> None:
        self._consecutive_failures += 1

    def _register_success(self) -> None:
        self._consecutive_failures = 0

    def _current_interval(self) -> float:
        if self._consecutive_failures == 0:
            return self._config.interval_seconds
        backoff = self._retry_policy.delay_for_attempt(self._consecutive_failures)
        logger.info(
            "Backing off after %d consecutive failed cycles: sleeping %.1fs",
            self._consecutive_failures,
            backoff,
        )
        return backoff
