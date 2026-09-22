"""VoidFix SMS gateway adapter (Phase 2 — disabled by default).

VoidFix (gateway.voidfix.com) relays SMS through Android devices running
its sender app. The documented send webhook is:

    POST https://sms.voidfix.com/services/send.php
    application/x-www-form-urlencoded
    form fields: number, devices, key, message

`devices` values are VoidFix device IDs assigned when a phone is enrolled
in the VoidFix dashboard — they are NOT ADB serials. Map bay -> device ID
in `voidfix_devices.json` (see `voidfix_devices.example.json`).

Inbound SMS on VoidFix is dashboard/socket based and can be forwarded to
an operator-owned HTTPS URL. There is no publicly documented receive URL,
so `fetch_inbound` stays inert until `VOIDFIX_INBOUND_ENDPOINT` is set.
Webhook bodies are parsed by `ingest_inbound` without contacting VoidFix.

Security rules enforced here:
- the gateway refuses to construct without an API key;
- send and inbound endpoints must be HTTPS;
- the API key and message bodies are never logged.
"""
from __future__ import annotations

import logging
from typing import Iterable

import requests

from domain.models import InboundSms, SmsSendOutcome, SmsSendResult
from domain.ports import SmsGateway

logger = logging.getLogger("mobi_rent_agent.voidfix")

_TRUE = {True, 1, "1", "true", "True", "TRUE"}
_FALSE = {False, 0, "0", "false", "False", "FALSE"}


class SmsGatewayError(RuntimeError):
    """The SMS gateway is misconfigured or an operation cannot proceed."""


class VoidFixSmsGateway(SmsGateway):
    def __init__(
        self,
        api_key: str,
        send_endpoint: str = "https://sms.voidfix.com/services/send.php",
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

    def send(self, to_number: str, message: str, device_ids: Iterable[str]) -> SmsSendResult:
        devices = [str(device_id).strip() for device_id in device_ids if str(device_id).strip()]
        if not to_number.strip():
            return SmsSendResult(success=False, to_number=to_number, error="recipient number is empty")
        if not message:
            return SmsSendResult(success=False, to_number=to_number, error="message body is empty")
        if not devices:
            return SmsSendResult(success=False, to_number=to_number, error="no sender device ids supplied")

        try:
            response = self._session.post(
                self._send_endpoint,
                data={
                    "number": to_number,
                    "devices": ",".join(devices),
                    "key": self._api_key,
                    "message": message,
                },
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
        if not self._inbound_endpoint:
            raise SmsGatewayError(
                "VoidFix inbound endpoint is not configured; set VOIDFIX_INBOUND_ENDPOINT "
                "once the account-specific receive URL is known"
            )
        try:
            response = self._session.get(
                self._inbound_endpoint,
                params={"key": self._api_key},
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            raise SmsGatewayError(f"inbound fetch failed: {exc}") from exc

        if isinstance(body, dict) and body.get("success") in _FALSE:
            raise SmsGatewayError(_extract_error(body) or "provider reported success=false")
        return self.ingest_inbound(body)

    def ingest_inbound(self, payload: object) -> Iterable[InboundSms]:
        return parse_inbound_payload(payload)

    def close(self) -> None:
        self._session.close()


def interpret_send_response(
    response: requests.Response,
    to_number: str,
    *,
    secret: str | None = None,
) -> SmsSendResult:
    """Turn an HTTP response into `SmsSendResult` without logging bodies.

    HTTP 2xx is not enough. VoidFix-style bodies use a ``success`` flag;
    a missing or unreadable flag is ``UNKNOWN``, not delivery.
    """
    status = response.status_code
    body = _try_json(response)
    message_id = _extract_message_id(body)

    if not (200 <= status < 300):
        return SmsSendResult(
            success=False,
            to_number=to_number,
            status_code=status,
            provider_message_id=message_id,
            error=_redact(_extract_error(body) or _safe_body(response), secret),
            outcome=SmsSendOutcome.FAILURE,
        )

    if not isinstance(body, dict) or "success" not in body:
        logger.info("SMS provider response was ambiguous (HTTP %s)", status)
        return SmsSendResult(
            success=False,
            to_number=to_number,
            status_code=status,
            provider_message_id=message_id,
            error="unknown: provider response missing success flag",
            outcome=SmsSendOutcome.UNKNOWN,
        )

    flag = body.get("success")
    if flag in _FALSE:
        logger.info("SMS rejected by gateway (HTTP %s)", status)
        return SmsSendResult(
            success=False,
            to_number=to_number,
            status_code=status,
            provider_message_id=message_id,
            error=_redact(_extract_error(body) or "provider reported success=false", secret),
            outcome=SmsSendOutcome.FAILURE,
        )
    if flag not in _TRUE:
        logger.info("SMS provider success flag was ambiguous (HTTP %s)", status)
        return SmsSendResult(
            success=False,
            to_number=to_number,
            status_code=status,
            provider_message_id=message_id,
            error="unknown: provider success field is not a recognized boolean",
            outcome=SmsSendOutcome.UNKNOWN,
        )

    logger.info("SMS accepted by gateway via parsed send response")
    return SmsSendResult(
        success=True,
        to_number=to_number,
        status_code=status,
        provider_message_id=message_id,
        outcome=SmsSendOutcome.SUCCESS,
    )


def parse_inbound_payload(payload: object) -> list[InboundSms]:
    """Parse a webhook body, poll JSON, or a single message object."""
    raw_messages = _coerce_message_list(payload)
    messages: list[InboundSms] = []
    for index, raw in enumerate(raw_messages):
        if not isinstance(raw, dict):
            raise SmsGatewayError(f"inbound message at index {index} must be an object")
        from_number = _first_str(raw, ("number", "from_number", "from", "sender"))
        message = _first_str(raw, ("message", "text", "body"))
        if from_number is None or message is None:
            raise SmsGatewayError(
                f"inbound message at index {index} is missing number/message fields"
            )
        messages.append(
            InboundSms(
                from_number=from_number,
                message=message,
                device_id=_first_str(raw, ("device", "device_id", "deviceId", "deviceID")),
                received_at=_first_str(raw, ("received_at", "receivedAt", "timestamp", "date")),
            )
        )
    return messages


def _coerce_message_list(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise SmsGatewayError("inbound response must be a list or a JSON object")
    if isinstance(payload.get("messages"), list):
        return payload["messages"]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"]
    if isinstance(data, list):
        return data
    if _looks_like_message(payload):
        return [payload]
    raise SmsGatewayError("inbound response must be a list or an object containing 'messages'")


def _looks_like_message(raw: dict) -> bool:
    has_from = any(key in raw for key in ("number", "from_number", "from", "sender"))
    has_body = any(key in raw for key in ("message", "text", "body"))
    return has_from and has_body


def _first_str(raw: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if raw.get(key) is not None:
            return str(raw[key])
    return None


def _try_json(response: requests.Response) -> object | None:
    try:
        return response.json()
    except ValueError:
        return None


def _extract_message_id(body: object | None) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("id", "message_id", "messageId", "ID"):
        if body.get(key) is not None:
            return str(body[key])
    data = body.get("data")
    candidates: list = []
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list):
            candidates = messages
        elif data.get("ID") is not None or data.get("id") is not None:
            candidates = [data]
    elif isinstance(data, list):
        candidates = data
    if candidates and isinstance(candidates[0], dict):
        first = candidates[0]
        for key in ("ID", "id", "message_id", "messageId"):
            if first.get(key) is not None:
                return str(first[key])
    return None


def _extract_error(body: object | None) -> str | None:
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        for key in ("message", "code", "error"):
            if err.get(key) is not None:
                return str(err[key])
        return "provider error object"
    if err:
        return str(err)
    if body.get("success") in _FALSE and body.get("message") is not None:
        return str(body["message"])
    return None


def _safe_body(response: requests.Response, limit: int = 300) -> str:
    try:
        return response.text[:limit]
    except Exception:  # pragma: no cover - defensive
        return "<unreadable response body>"


def _redact(text: str, secret: str | None) -> str:
    if not text or not secret:
        return text
    return text.replace(secret, "<redacted>")
