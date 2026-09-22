"""Prototype-only VoidFix REST client (JSON send.php contract).

This module is isolated from production `VoidFixSmsGateway`. Do not import
this from `main.py` or the farm SMS dispatcher.

Confirmed prototype request:

    POST https://sms.voidfix.com/services/send.php
    Content-Type: application/json
    {
      "key": "<API_KEY>",
      "number": "<RECIPIENT>",
      "message": "<MESSAGE>",
      "devices": [1385],
      "slot": 1
    }

VoidFix Support confirmed that ``slot=1`` selects SIM 2. ``slot`` must be
derived from a live Android ``simSlotIndex`` check (US Mobile = 1). This
client must not invent ``sim``, ``simSlot``, ``sim_slot``, ``device_id``,
``sendto``, ``body``, or ``token``, and must not encode ``devices`` as
``1385|1`` or ``[1385, 1]``. It must not fall back to slot 0.
"""
from __future__ import annotations

from typing import Any, Iterable

import requests

from domain.models import InboundSms, SmsSendOutcome, SmsSendResult
from domain.ports import SmsGateway
from domain.prototype import PROTOTYPE_DEVICE_ID, PROTOTYPE_VOIDFIX_DEVICE_ID
from domain.verification import FourLayerVerification
from infrastructure.voidfix_api import SmsGatewayError, interpret_send_response, parse_inbound_payload

DEFAULT_SEND_ENDPOINT = "https://sms.voidfix.com/services/send.php"
JSON_CONTENT_TYPE = "application/json"
SEND_PHP_HAS_CONFIRMED_SLOT_PARAMETER = True
REQUIRED_ANDROID_SIM_SLOT_INDEX = 1
REQUIRED_API_SLOT = 1
FORBIDDEN_REQUEST_FIELDS = frozenset(
    {"sim", "simSlot", "sim_slot", "device_id", "sendto", "body", "token"}
)
MISSING_LIVE_SLOT_REASON = (
    "live Android simSlotIndex is required; refusing to hardcode VoidFix slot"
)
SLOT_ZERO_REFUSED_REASON = (
    "refusing Android/VoidFix slot 0; US Mobile SIM 2 requires simSlotIndex=1 and slot=1"
)
HARMLESS_TEST_SMS = "Mobi-Rent prototype SMS test - please reply TEST"


class PrototypeVoidFixRestClient(SmsGateway):
    """JSON send.php client for the isolated Pixel 7a prototype only."""

    def __init__(
        self,
        api_key: str,
        send_endpoint: str = DEFAULT_SEND_ENDPOINT,
        inbound_endpoint: str | None = None,
        timeout_seconds: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise SmsGatewayError("VoidFix API key must not be empty")
        if not send_endpoint.lower().startswith("https://"):
            raise SmsGatewayError("VoidFix send endpoint must use HTTPS")
        if inbound_endpoint and not inbound_endpoint.lower().startswith("https://"):
            raise SmsGatewayError("VoidFix inbound endpoint must use HTTPS")
        self._api_key = api_key
        self._send_endpoint = send_endpoint
        self._inbound_endpoint = inbound_endpoint
        self._timeout = timeout_seconds
        self._session = session or requests.Session()

    def build_json_payload(
        self,
        to_number: str,
        message: str,
        device_ids: Iterable[str],
        *,
        slot: int,
    ) -> dict[str, Any]:
        devices = _parse_prototype_device_ids(device_ids)
        slot_value = voidfix_slot_from_live_android(slot)
        payload = {
            "key": self._api_key,
            "number": to_number,
            "message": message,
            "devices": devices,
            "slot": slot_value,
        }
        unexpected = FORBIDDEN_REQUEST_FIELDS.intersection(payload)
        if unexpected:
            raise SmsGatewayError(f"refusing undocumented send.php fields: {sorted(unexpected)}")
        return payload

    def send(
        self,
        to_number: str,
        message: str,
        device_ids: Iterable[str],
        *,
        sim_slot: int | None = None,
    ) -> SmsSendResult:
        return SmsSendResult(
            success=False,
            to_number=to_number,
            error=MISSING_LIVE_SLOT_REASON,
            outcome=SmsSendOutcome.FAILURE,
        )

    def send_with_live_android_slot(
        self,
        to_number: str,
        message: str,
        device_ids: Iterable[str],
        android_sim_slot_index: int | None,
    ) -> SmsSendResult:
        """POST only after a live Android slot check. Does not retry."""
        return self.execute_documented_send(
            to_number,
            message,
            device_ids,
            slot=android_sim_slot_index,
        )

    def execute_documented_send(
        self,
        to_number: str,
        message: str,
        device_ids: Iterable[str],
        *,
        slot: int | None,
    ) -> SmsSendResult:
        """POST the confirmed JSON body. Unit tests use a fake session."""
        local_error = _local_send_error(to_number, message, device_ids, slot)
        if local_error:
            return SmsSendResult(success=False, to_number=to_number, error=local_error)
        payload = self.build_json_payload(to_number, message, device_ids, slot=slot)
        try:
            response = self._session.post(
                self._send_endpoint,
                json=payload,
                headers={"Content-Type": JSON_CONTENT_TYPE, "Accept": JSON_CONTENT_TYPE},
                timeout=self._timeout,
            )
        except requests.exceptions.Timeout as exc:
            return SmsSendResult(
                success=False,
                to_number=to_number,
                error=_redact(f"timeout: {exc}", self._api_key),
            )
        except requests.exceptions.RequestException as exc:
            return SmsSendResult(
                success=False,
                to_number=to_number,
                error=_redact(f"request_error: {exc}", self._api_key),
            )
        return interpret_send_response(response, to_number, secret=self._api_key)

    def fetch_inbound(self) -> Iterable[InboundSms]:
        raise SmsGatewayError(
            "prototype inbound endpoint is not configured; inbound stays "
            "unsupported_pending_provider_documentation"
        )

    def ingest_inbound(self, payload: object) -> Iterable[InboundSms]:
        return parse_inbound_payload(payload)

    def close(self) -> None:
        self._session.close()


def live_android_sim_slot_index(verification: FourLayerVerification | None) -> int | None:
    """Read-only extraction of Android simSlotIndex. Does not hardcode slot 1."""
    if verification is None:
        return None
    return verification.subscription.sim_slot_index


def voidfix_slot_from_live_android(android_sim_slot_index: int | None) -> int:
    """Map a live Android simSlotIndex to the confirmed VoidFix slot field.

    Slot is not a production-wide hardcoded default. For this prototype it
    is valid only when the live US Mobile SIM reports simSlotIndex=1, which
    VoidFix Support confirmed is API ``slot=1`` (SIM 2).
    """
    if android_sim_slot_index is None:
        raise SmsGatewayError(MISSING_LIVE_SLOT_REASON)
    if android_sim_slot_index == 0:
        raise SmsGatewayError(SLOT_ZERO_REFUSED_REASON)
    if android_sim_slot_index != REQUIRED_ANDROID_SIM_SLOT_INDEX:
        raise SmsGatewayError(
            f"live Android simSlotIndex is {android_sim_slot_index}, "
            f"expected {REQUIRED_ANDROID_SIM_SLOT_INDEX} for US Mobile SIM 2"
        )
    return android_sim_slot_index


def _local_send_error(
    to_number: str,
    message: str,
    device_ids: Iterable[str],
    slot: int | None,
) -> str | None:
    if not to_number.strip():
        return "recipient number is empty"
    if not message:
        return "message body is empty"
    try:
        _parse_prototype_device_ids(device_ids)
        voidfix_slot_from_live_android(slot)
    except SmsGatewayError as exc:
        return str(exc)
    return None


def _parse_prototype_device_ids(device_ids: Iterable[str]) -> list[int]:
    parsed: list[int] = []
    for raw in device_ids:
        token = str(raw).strip()
        if not token:
            continue
        if any(marker in token for marker in ("|", ",", "[", "]")):
            raise SmsGatewayError(
                "devices must be a JSON array of numeric VoidFix IDs; "
                "refusing 1385|1, [1385,1], or comma-separated encodings"
            )
        if not token.isdigit():
            raise SmsGatewayError("VoidFix device IDs must be numeric")
        value = int(token)
        if _is_farm_slot_id(value) and value != PROTOTYPE_VOIDFIX_DEVICE_ID:
            raise SmsGatewayError(f"production farm slot ID {value} is not allowed")
        parsed.append(value)
    if not parsed:
        raise SmsGatewayError("no sender device ids supplied")
    if parsed != [PROTOTYPE_VOIDFIX_DEVICE_ID]:
        raise SmsGatewayError(
            f"prototype client accepts only VoidFix device {PROTOTYPE_VOIDFIX_DEVICE_ID}"
        )
    return parsed


def _is_farm_slot_id(value: int) -> bool:
    return 1 <= value <= 20


def _redact(text: str, secret: str | None) -> str:
    if not text or not secret:
        return text
    return text.replace(secret, "<redacted>")


def assert_slot_parameter_selects_sim2() -> None:
    """Documentation assertion: confirmed slot=1 means SIM 2 / Android slot 1."""
    if not SEND_PHP_HAS_CONFIRMED_SLOT_PARAMETER:
        raise AssertionError("VoidFix slot parameter is not marked confirmed")
    if REQUIRED_API_SLOT != 1 or REQUIRED_ANDROID_SIM_SLOT_INDEX != 1:
        raise AssertionError("prototype slot mapping drifted from SIM 2 / Android slot 1")
    _ = PROTOTYPE_DEVICE_ID
