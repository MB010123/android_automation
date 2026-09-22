"""Slot-scoped SMS dispatch over the existing `SmsGateway` port.

Does not run from the daemon loop. Callers must construct this service
explicitly. Sends are refused unless the slot is on the VoidFix allowlist
and has a dashboard device ID.
"""
from __future__ import annotations

import hmac
import logging

from application.slot_coordinator import SlotOperationCoordinator
from domain.models import InboundSms, SmsSendResult
from domain.ports import SmsGateway
from domain.slot_isolation import SlotIsolationError, SlotIsolationPolicy

logger = logging.getLogger("mobi_rent_agent.sms")


class SmsDispatchService:
    def __init__(
        self,
        gateway: SmsGateway,
        isolation: SlotIsolationPolicy,
        device_map: dict[int, str],
        coordinator: SlotOperationCoordinator | None = None,
        webhook_secret: str | None = None,
    ) -> None:
        self._gateway = gateway
        self._isolation = isolation
        self._device_map = dict(device_map)
        self._coordinator = coordinator
        self._webhook_secret = webhook_secret or None

    def send_for_slot(self, slot_id: int, to_number: str, message: str) -> SmsSendResult:
        try:
            self._isolation.reject_if_outside(slot_id)
        except SlotIsolationError as exc:
            return SmsSendResult(success=False, to_number=to_number, error=str(exc))

        device_id = self._device_map.get(int(slot_id))
        if not device_id:
            return SmsSendResult(
                success=False,
                to_number=to_number,
                error=f"slot {slot_id} has no VoidFix device_id in the device map",
            )

        logger.info("dispatching SMS for allowed slot %s", slot_id)
        if self._coordinator is None:
            return self._gateway.send(to_number, message, [device_id])
        try:
            with self._coordinator.acquire(slot_id, blocking=False) as acquired:
                if not acquired:
                    return SmsSendResult(
                        success=False,
                        to_number=to_number,
                        error=f"slot {slot_id} is busy",
                    )
                return self._gateway.send(to_number, message, [device_id])
        except KeyError:
            return SmsSendResult(
                success=False,
                to_number=to_number,
                error=f"slot {slot_id} is not in the slot map",
            )

    def fetch_inbound(self) -> list[InboundSms]:
        return list(self._gateway.fetch_inbound())

    def ingest_inbound(
        self,
        payload: object,
        provided_secret: str | None = None,
    ) -> list[InboundSms]:
        if self._webhook_secret:
            if provided_secret is None or not hmac.compare_digest(
                provided_secret, self._webhook_secret
            ):
                raise ValueError("inbound webhook secret mismatch")
        return list(self._gateway.ingest_inbound(payload))
