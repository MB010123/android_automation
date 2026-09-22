"""Slot-scoped SMS dispatch over the existing `SmsGateway` port.

Does not run from the daemon loop. Callers must construct this service
explicitly. Live sends are refused unless the slot is uniquely verified,
VoidFix-allowlisted, recipient-allowlisted, and live-send authorized.
"""
from __future__ import annotations

import hmac
import logging
import time
import uuid

from application.slot_coordinator import SlotOperationCoordinator
from application.slot_identity import SlotIdentityError, SlotIdentityGuard
from application.sms_outbox import DuplicateIdempotencyError, SmsOutbox
from domain.farm import (
    OutboundMessageStatus,
    SmsFinalStatus,
    SmsRetryPolicy,
    VoidFixDeliveryPollPolicy,
    is_safe_retryable_sms,
    is_terminal_sms_final_status,
)
from infrastructure.voidfix_delivery import (
    VoidFixDeliveryPoller,
    final_status_to_outbound,
    is_delivered,
    snapshot_from_row,
)
from domain.models import InboundSms, SmsSendOutcome, SmsSendResult
from domain.ports import SmsGateway
from domain.slot_isolation import SlotIsolationError, SlotIsolationPolicy
from infrastructure.redact import normalize_msisdn, redact_phone

logger = logging.getLogger("mobi_rent_agent.sms")


class SmsDispatchService:
    def __init__(
        self,
        gateway: SmsGateway,
        isolation: SlotIsolationPolicy,
        device_map: dict[int, str],
        coordinator: SlotOperationCoordinator | None = None,
        webhook_secret: str | None = None,
        recipient_allowlist: tuple[str, ...] | list[str] | None = None,
        live_send_authorized: bool = False,
        dry_run: bool = True,
        outbox: SmsOutbox | None = None,
        identity_guard: SlotIdentityGuard | None = None,
        retry_policy: SmsRetryPolicy | None = None,
        sleeper=None,
        delivery_poller: VoidFixDeliveryPoller | None = None,
        delivery_policy: VoidFixDeliveryPollPolicy | None = None,
        voidfix_sim_slots: dict[int, int] | None = None,
        monotonic=None,
    ) -> None:
        self._gateway = gateway
        self._isolation = isolation
        self._device_map = dict(device_map)
        slot_ids = sorted(set(self._device_map) | set(isolation.allowed_slot_ids))
        self._coordinator = coordinator or SlotOperationCoordinator(slot_ids)
        self._webhook_secret = webhook_secret or None
        self._recipient_allowlist = {
            normalize_msisdn(item) for item in (recipient_allowlist or ()) if str(item).strip()
        }
        self._live_send_authorized = live_send_authorized
        self._dry_run = dry_run
        self._outbox = outbox or SmsOutbox()
        self._identity_guard = identity_guard
        self._retry_policy = retry_policy or SmsRetryPolicy()
        self._sleeper = sleeper or (lambda _seconds: None)
        self._delivery_poller = delivery_poller
        self._delivery_policy = delivery_policy or VoidFixDeliveryPollPolicy()
        self._voidfix_sim_slots = dict(voidfix_sim_slots or {})
        self._monotonic = monotonic or time.monotonic

    def resume_inflight_delivery_tracking(self) -> list[str]:
        """Poll VoidFix for in-flight records after restart. Never calls send."""
        resumed: list[str] = []
        for record in self._outbox.list_non_terminal():
            if is_terminal_sms_final_status(record.final_status):
                continue
            if record.provider_message_id and self._delivery_poller is not None:
                logger.info(
                    "sms outbox resume poll slot=%s key=%s provider_id=%s status=%s",
                    record.slot_id,
                    record.idempotency_key,
                    record.provider_message_id,
                    record.status.value,
                )
                seed = SmsSendResult(
                    success=True,
                    to_number="",
                    provider_message_id=record.provider_message_id,
                )
                self._poll_delivery(record.idempotency_key, seed)
                resumed.append(record.idempotency_key)
                continue
            logger.info(
                "sms outbox recovered non-terminal record slot=%s key=%s status=%s "
                "(no provider id; will not resend)",
                record.slot_id,
                record.idempotency_key,
                record.status.value,
            )
        return resumed

    def send_for_slot(
        self,
        slot_id: int,
        to_number: str,
        message: str,
        *,
        idempotency_key: str | None = None,
        expected_voidfix_device_id: str | None = None,
        job_id: str | None = None,
    ) -> SmsSendResult:
        job = job_id or str(uuid.uuid4())
        key = idempotency_key or job
        existing = self._outbox.get(key)
        if existing is not None:
            logger.info(
                "sms duplicate idempotency_key slot=%s job=%s status=%s",
                slot_id,
                existing.job_id,
                existing.status.value,
            )
            return SmsSendResult(
                success=False,
                to_number=to_number,
                provider_message_id=existing.provider_message_id,
                error=f"duplicate idempotency key status={existing.status.value}",
                outcome=(
                    SmsSendOutcome.UNKNOWN
                    if existing.status is OutboundMessageStatus.UNKNOWN
                    else SmsSendOutcome.FAILURE
                ),
            )

        blocked = self._block_reason(
            slot_id, to_number, expected_voidfix_device_id=expected_voidfix_device_id
        )
        if blocked:
            self._record_blocked(key, job, slot_id, to_number, blocked)
            return SmsSendResult(success=False, to_number=to_number, error=blocked)

        try:
            record = self._outbox.reserve(
                slot_id=slot_id,
                to_number=to_number,
                idempotency_key=key,
                job_id=job,
            )
        except DuplicateIdempotencyError as exc:
            return SmsSendResult(success=False, to_number=to_number, error=str(exc))

        if self._dry_run or not self._live_send_authorized:
            reason = "dry-run: provider not contacted"
            self._outbox.update(
                record.idempotency_key,
                status=OutboundMessageStatus.DRY_RUN,
                blocked_reason=reason,
            )
            logger.info(
                "sms dry-run slot=%s job=%s status=%s recipient=%s",
                slot_id,
                job,
                OutboundMessageStatus.DRY_RUN.value,
                redact_phone(to_number),
            )
            return SmsSendResult(success=False, to_number=to_number, error=reason)

        device_id = self._device_map[int(slot_id)]
        try:
            with self._coordinator.acquire(slot_id, blocking=False) as acquired:
                if not acquired:
                    self._outbox.update(
                        record.idempotency_key,
                        status=OutboundMessageStatus.BLOCKED,
                        blocked_reason=f"slot {slot_id} is busy",
                    )
                    return SmsSendResult(
                        success=False,
                        to_number=to_number,
                        error=f"slot {slot_id} is busy",
                    )
                result = self._submit_with_retries(to_number, message, device_id, slot_id)
        except KeyError:
            self._outbox.update(
                record.idempotency_key,
                status=OutboundMessageStatus.BLOCKED,
                blocked_reason=f"slot {slot_id} is not in the slot map",
            )
            return SmsSendResult(
                success=False,
                to_number=to_number,
                error=f"slot {slot_id} is not in the slot map",
            )

        if result.success and result.provider_message_id:
            accept = result.provider_accept_row or {}
            self._outbox.update(
                record.idempotency_key,
                status=OutboundMessageStatus.CONFIRMED,
                provider_message_id=result.provider_message_id,
                voidfix_device_id=str(device_id),
                voidfix_sim_slot=result.voidfix_sim_slot
                or self._voidfix_sim_slots.get(int(slot_id)),
                accepted_at=time.time(),
                provider_sent_date=_str_field(accept.get("sentDate")),
                provider_error_code=_str_field(accept.get("errorCode")),
                final_status=SmsFinalStatus.ACCEPTED,
                error=result.error,
            )
            if self._delivery_poller is not None:
                result = self._poll_delivery(record.idempotency_key, result)
            else:
                logger.info(
                    "sms result slot=%s job=%s status=%s provider_id=%s error=%s",
                    slot_id,
                    job,
                    OutboundMessageStatus.CONFIRMED.value,
                    result.provider_message_id or "",
                    result.error or "",
                )
                return result
        else:
            status = _status_from_result(result)
            self._outbox.update(
                record.idempotency_key,
                status=status,
                provider_message_id=result.provider_message_id,
                error=result.error,
                final_status=SmsFinalStatus.FAILED if status is OutboundMessageStatus.FAILED else None,
            )

        record_after = self._outbox.get(record.idempotency_key)
        logger.info(
            "sms result slot=%s job=%s status=%s final=%s provider_id=%s error=%s",
            slot_id,
            job,
            record_after.status.value if record_after else "",
            record_after.final_status.value if record_after and record_after.final_status else "",
            result.provider_message_id or "",
            result.error or "",
        )
        return result

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

    def _block_reason(
        self,
        slot_id: int,
        to_number: str,
        *,
        expected_voidfix_device_id: str | None,
    ) -> str | None:
        if slot_id is None:
            return "slot id is required"
        try:
            slot_id = int(slot_id)
        except (TypeError, ValueError):
            return "slot id is required"
        try:
            self._isolation.reject_if_outside(slot_id)
        except SlotIsolationError as exc:
            return str(exc)

        if self._identity_guard is not None:
            try:
                self._identity_guard.verify(slot_id)
            except SlotIdentityError as exc:
                return str(exc)

        device_id = self._device_map.get(int(slot_id))
        if not device_id:
            return f"slot {slot_id} has no VoidFix device_id in the device map"
        if expected_voidfix_device_id and str(expected_voidfix_device_id) != str(device_id):
            return f"slot {slot_id} VoidFix device_id does not match the expected id"
        if not to_number or not str(to_number).strip():
            return "recipient number is empty"
        if not self._recipient_allowlist:
            return "recipient allowlist is empty"
        if normalize_msisdn(to_number) not in self._recipient_allowlist:
            return "recipient is not on the allowlist"
        if not self._live_send_authorized and not self._dry_run:
            return "live SMS is not authorized"
        return None

    def _record_blocked(
        self,
        key: str,
        job: str,
        slot_id: int,
        to_number: str,
        reason: str,
    ) -> None:
        try:
            self._outbox.reserve(
                slot_id=slot_id,
                to_number=to_number,
                idempotency_key=key,
                job_id=job,
            )
            self._outbox.update(
                key,
                status=OutboundMessageStatus.BLOCKED,
                blocked_reason=reason,
                error=reason,
            )
        except DuplicateIdempotencyError:
            pass
        logger.info(
            "sms blocked slot=%s job=%s status=%s reason=%s recipient=%s",
            slot_id,
            job,
            OutboundMessageStatus.BLOCKED.value,
            reason,
            redact_phone(to_number),
        )

    def _poll_delivery(self, idempotency_key: str, result: SmsSendResult) -> SmsSendResult:
        assert self._delivery_poller is not None
        provider_id = result.provider_message_id
        assert provider_id

        def on_poll(snap) -> None:
            stamp = time.time()
            current = self._outbox.get(idempotency_key)
            status = current.status if current else OutboundMessageStatus.CONFIRMED
            final_status = current.final_status if current else SmsFinalStatus.ACCEPTED
            kwargs: dict = {"last_polled_at": stamp, "status": status, "final_status": final_status}
            if snap is not None:
                kwargs.update(
                    {
                        "provider_sent_date": snap.sent_date or None,
                        "provider_delivered_date": snap.delivered_date or None,
                        "provider_error_code": snap.error_code,
                        "voidfix_device_id": snap.device_id,
                        "voidfix_sim_slot": snap.sim_slot,
                    }
                )
                if is_delivered(snap.raw):
                    kwargs["final_status"] = SmsFinalStatus.DELIVERED
                    kwargs["status"] = OutboundMessageStatus.DELIVERED
                elif snap.status and snap.status.lower() == "failed":
                    kwargs["final_status"] = SmsFinalStatus.FAILED
                    kwargs["status"] = OutboundMessageStatus.FAILED
                elif snap.status and snap.status.lower() in {"sent", "pending"}:
                    kwargs["final_status"] = SmsFinalStatus.SENT
                    kwargs["status"] = OutboundMessageStatus.SENT
            self._outbox.update(idempotency_key, **kwargs)

        poll = self._delivery_poller.poll_until_terminal(
            provider_id,
            policy=self._delivery_policy,
            sleeper=self._sleeper,
            monotonic=self._monotonic,
            on_poll=on_poll,
        )
        snap = poll.snapshot
        outbound = final_status_to_outbound(poll.final_status)
        update_kwargs = {
            "status": outbound,
            "final_status": poll.final_status,
            "last_polled_at": time.time(),
        }
        if snap is not None:
            update_kwargs.update(
                {
                    "provider_sent_date": snap.sent_date,
                    "provider_delivered_date": snap.delivered_date,
                    "provider_error_code": snap.error_code,
                    "voidfix_device_id": snap.device_id,
                    "voidfix_sim_slot": snap.sim_slot,
                }
            )
        if poll.final_status is SmsFinalStatus.TIMEOUT:
            update_kwargs["error"] = "delivery poll timed out before confirmation"
        self._outbox.update(idempotency_key, **update_kwargs)
        if poll.final_status is SmsFinalStatus.DELIVERED:
            return result
        if poll.final_status is SmsFinalStatus.FAILED:
            return SmsSendResult(
                success=False,
                to_number=result.to_number,
                provider_message_id=provider_id,
                error="provider reported failed delivery",
                outcome=SmsSendOutcome.FAILURE,
            )
        return SmsSendResult(
            success=False,
            to_number=result.to_number,
            provider_message_id=provider_id,
            error=update_kwargs.get("error") or "delivery status unknown after poll timeout",
            outcome=SmsSendOutcome.UNKNOWN,
        )

    def _submit_with_retries(
        self,
        to_number: str,
        message: str,
        device_id: str,
        slot_id: int,
    ) -> SmsSendResult:
        sim_slot = self._voidfix_sim_slots.get(int(slot_id))
        last = SmsSendResult(success=False, to_number=to_number, error="no attempt")
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            last = self._gateway.send(to_number, message, [device_id], sim_slot=sim_slot)
            if last.success:
                return last
            outcome = (last.outcome.value if last.outcome else "failure")
            if not is_safe_retryable_sms(outcome, last.error):
                return last
            if attempt >= self._retry_policy.max_attempts:
                return last
            self._sleeper(self._retry_policy.delay_for_attempt(attempt))
        return last


def _str_field(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _status_from_result(result: SmsSendResult) -> OutboundMessageStatus:
    if result.success and result.provider_message_id:
        return OutboundMessageStatus.CONFIRMED
    if result.outcome is SmsSendOutcome.UNKNOWN:
        return OutboundMessageStatus.UNKNOWN
    if result.success:
        return OutboundMessageStatus.SUBMITTED
    return OutboundMessageStatus.FAILED
