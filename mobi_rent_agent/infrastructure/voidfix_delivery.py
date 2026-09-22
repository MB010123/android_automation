"""VoidFix outbound delivery status via read-messages.php (not get-messages.php)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import requests

from domain.farm import SmsFinalStatus, VoidFixDeliveryPollPolicy
from infrastructure.voidfix_api import SmsGatewayError, _coerce_message_list, _try_json

logger = logging.getLogger("mobi_rent_agent.voidfix.delivery")

DEFAULT_READ_MESSAGES_ENDPOINT = "https://sms.voidfix.com/services/read-messages.php"

_NO_ERROR_CODES = frozenset({None, "", 0, "0", -1, "-1"})


@dataclass(frozen=True)
class VoidFixMessageSnapshot:
    message_id: str
    status: str | None
    device_id: str | None
    sim_slot: int | None
    destination: str | None
    sent_date: str | None
    delivered_date: str | None
    error_code: str | None
    raw: dict


@dataclass(frozen=True)
class DeliveryPollResult:
    final_status: SmsFinalStatus
    snapshot: VoidFixMessageSnapshot | None
    timed_out: bool = False


def snapshot_from_row(row: dict) -> VoidFixMessageSnapshot:
    msg_id = str(row.get("ID") or row.get("id") or "")
    sim = row.get("simSlot")
    return VoidFixMessageSnapshot(
        message_id=msg_id,
        status=str(row.get("status") or "") or None,
        device_id=_str_or_none(row.get("deviceID", row.get("deviceId"))),
        sim_slot=int(sim) if sim is not None and str(sim).strip() != "" else None,
        destination=_str_or_none(row.get("number")),
        sent_date=_str_or_none(row.get("sentDate")),
        delivered_date=_str_or_none(row.get("deliveredDate")),
        error_code=_str_or_none(row.get("errorCode")),
        raw=dict(row),
    )


def classify_delivery(row: dict | VoidFixMessageSnapshot) -> SmsFinalStatus | None:
    """Return terminal/in-progress status, or None to keep polling."""
    snap = row if isinstance(row, VoidFixMessageSnapshot) else snapshot_from_row(row)
    if is_delivery_failed(snap.raw):
        return SmsFinalStatus.FAILED
    if is_delivered(snap.raw):
        return SmsFinalStatus.DELIVERED
    status = (snap.status or "").lower()
    if status in {"sent", "pending", "queued"}:
        return SmsFinalStatus.SENT
    if status:
        return SmsFinalStatus.SENT
    return SmsFinalStatus.SENT


def is_delivered(row: dict) -> bool:
    delivered = row.get("deliveredDate")
    if delivered is None:
        return False
    text = str(delivered).strip()
    return bool(text) and text.lower() not in {"null", "none"}


def is_delivery_failed(row: dict) -> bool:
    status = str(row.get("status") or "").lower()
    if status in {"failed", "canceled", "cancelled", "error"}:
        return True
    err = row.get("errorCode")
    if err in _NO_ERROR_CODES:
        return False
    if err is not None and str(err).strip() != "":
        return True
    return False


def find_message_by_id(payload: object, message_id: str) -> dict | None:
    target = str(message_id).strip()
    if not target:
        return None
    try:
        messages = _coerce_message_list(payload)
    except SmsGatewayError:
        return None
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        mid = str(raw.get("ID") or raw.get("id") or "")
        if mid == target:
            return raw
    return None


def parse_read_messages_response(response: requests.Response) -> object:
    if not (200 <= response.status_code < 300):
        raise SmsGatewayError(f"read-messages HTTP {response.status_code}")
    body = _try_json(response)
    if body is None:
        raise SmsGatewayError("read-messages response was not JSON")
    if isinstance(body, dict) and body.get("success") in {False, 0, "0", "false"}:
        raise SmsGatewayError("read-messages reported success=false")
    return body


class VoidFixDeliveryPoller:
    def __init__(
        self,
        api_key: str,
        *,
        read_messages_endpoint: str = DEFAULT_READ_MESSAGES_ENDPOINT,
        timeout_seconds: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key.strip():
            raise SmsGatewayError("VoidFix API key must not be empty")
        if not read_messages_endpoint.lower().startswith("https://"):
            raise SmsGatewayError("read-messages endpoint must use HTTPS")
        self._api_key = api_key
        self._endpoint = read_messages_endpoint
        self._timeout = timeout_seconds
        self._session = session or requests.Session()

    def fetch_messages_payload(self) -> object:
        response = self._session.post(
            self._endpoint,
            data={"key": self._api_key},
            timeout=self._timeout,
        )
        return parse_read_messages_response(response)

    def find_message(self, provider_message_id: str) -> dict | None:
        payload = self.fetch_messages_payload()
        return find_message_by_id(payload, provider_message_id)

    def poll_until_terminal(
        self,
        provider_message_id: str,
        *,
        policy: VoidFixDeliveryPollPolicy | None = None,
        sleeper: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        on_poll: Callable[[VoidFixMessageSnapshot | None], None] | None = None,
    ) -> DeliveryPollResult:
        poll_policy = policy or VoidFixDeliveryPollPolicy()
        sleep = sleeper or time.sleep
        now = monotonic or time.monotonic
        deadline = now() + poll_policy.timeout_seconds
        last_snap: VoidFixMessageSnapshot | None = None

        while True:
            row = self.find_message(provider_message_id)
            last_snap = snapshot_from_row(row) if row else None
            if on_poll is not None:
                on_poll(last_snap)

            if row is not None:
                if is_delivery_failed(row):
                    return DeliveryPollResult(
                        final_status=SmsFinalStatus.FAILED,
                        snapshot=last_snap,
                    )
                if is_delivered(row):
                    return DeliveryPollResult(
                        final_status=SmsFinalStatus.DELIVERED,
                        snapshot=last_snap,
                    )

            if now() >= deadline:
                logger.info(
                    "delivery poll timed out for provider_message_id=%s",
                    provider_message_id,
                )
                return DeliveryPollResult(
                    final_status=SmsFinalStatus.TIMEOUT,
                    snapshot=last_snap,
                    timed_out=True,
                )

            sleep(poll_policy.interval_seconds)


def final_status_to_outbound(status: SmsFinalStatus):
    from domain.farm import OutboundMessageStatus

    return {
        SmsFinalStatus.QUEUED: OutboundMessageStatus.PENDING,
        SmsFinalStatus.ACCEPTED: OutboundMessageStatus.CONFIRMED,
        SmsFinalStatus.SENT: OutboundMessageStatus.SENT,
        SmsFinalStatus.DELIVERED: OutboundMessageStatus.DELIVERED,
        SmsFinalStatus.FAILED: OutboundMessageStatus.FAILED,
        SmsFinalStatus.TIMEOUT: OutboundMessageStatus.TIMEOUT,
    }[status]


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
