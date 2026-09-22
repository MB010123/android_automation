from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import SmsSendOutcome
from domain.ports import SmsGateway
from infrastructure.voidfix_api import (
    SmsGatewayError,
    VoidFixSmsGateway,
    interpret_send_response,
    parse_inbound_payload,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body=None, text: str = "") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.post_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def post(self, url, data=None, timeout=None):
        self.post_calls.append({"url": url, "data": data, "timeout": timeout})
        return self.response

    def get(self, url, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": params, "timeout": timeout})
        return self.response


SECRET = "voidfix-test-secret-do-not-log"


def make_gateway(response: FakeResponse, **kwargs) -> tuple[VoidFixSmsGateway, FakeSession]:
    session = FakeSession(response)
    gateway = VoidFixSmsGateway(api_key=kwargs.pop("api_key", SECRET), session=session, **kwargs)
    return gateway, session


def test_empty_api_key_is_rejected():
    with pytest.raises(SmsGatewayError):
        VoidFixSmsGateway(api_key="  ")


def test_send_endpoint_must_be_https():
    with pytest.raises(SmsGatewayError):
        VoidFixSmsGateway(api_key="k", send_endpoint="http://insecure.example/send.php")


def test_inbound_endpoint_must_be_https():
    with pytest.raises(SmsGatewayError):
        VoidFixSmsGateway(
            api_key="k",
            inbound_endpoint="http://insecure.example/inbound.php",
        )


def test_voidfix_gateway_implements_sms_gateway_port():
    assert issubclass(VoidFixSmsGateway, SmsGateway)


def test_successful_send_posts_documented_form_fields():
    gateway, session = make_gateway(FakeResponse(200, json_body={"success": True, "id": "msg-1"}))

    result = gateway.send("+15551234567", "hello", ["215"])

    assert result.success is True
    assert result.outcome is SmsSendOutcome.SUCCESS
    assert result.provider_message_id == "msg-1"
    sent = session.post_calls[0]
    assert sent["url"] == "https://sms.voidfix.com/services/send.php"
    assert sent["data"] == {
        "number": "+15551234567",
        "devices": "215",
        "key": SECRET,
        "message": "hello",
    }


def test_nested_data_messages_id_is_parsed():
    body = {"success": True, "data": {"messages": [{"ID": 99, "status": "Pending"}]}}
    gateway, _ = make_gateway(FakeResponse(200, json_body=body))

    result = gateway.send("+15551234567", "hello", ["215"])

    assert result.success is True
    assert result.outcome is SmsSendOutcome.SUCCESS
    assert result.provider_message_id == "99"


def test_http_200_with_success_false_is_failure():
    body = {"success": False, "error": {"code": 401, "message": "Invalid API key."}}
    gateway, _ = make_gateway(FakeResponse(200, json_body=body))

    result = gateway.send("+15551234567", "hello", ["215"])

    assert result.success is False
    assert result.outcome is SmsSendOutcome.FAILURE
    assert result.status_code == 200
    assert "Invalid API key" in (result.error or "")


def test_http_4xx_send_is_failure():
    gateway, _ = make_gateway(FakeResponse(401, text="unauthorized"))

    result = gateway.send("+15551234567", "hello", ["215"])

    assert result.success is False
    assert result.outcome is SmsSendOutcome.FAILURE
    assert result.status_code == 401
    assert "unauthorized" in (result.error or "")


def test_http_5xx_send_is_failure():
    gateway, _ = make_gateway(FakeResponse(503, text="unavailable"))

    result = gateway.send("+15551234567", "hello", ["215"])

    assert result.success is False
    assert result.outcome is SmsSendOutcome.FAILURE
    assert result.status_code == 503


def test_send_timeout_is_unknown_without_raising():
    session = FakeSession(FakeResponse(200))

    def boom(*_args, **_kwargs):
        raise requests.exceptions.Timeout("timed out")

    session.post = boom  # type: ignore[method-assign]
    gateway = VoidFixSmsGateway(api_key=SECRET, session=session)

    result = gateway.send("+15551234567", "hello", ["215"])

    assert result.success is False
    assert result.outcome is SmsSendOutcome.UNKNOWN
    assert "timeout" in (result.error or "")
    assert SECRET not in (result.error or "")


def test_success_without_provider_message_id_is_unknown():
    gateway, _ = make_gateway(FakeResponse(200, json_body={"success": True}))

    result = gateway.send("+15551234567", "hello", ["215"])

    assert result.success is False
    assert result.outcome is SmsSendOutcome.UNKNOWN
    assert "message id" in (result.error or "")


def test_send_rejects_missing_devices_locally():
    gateway, session = make_gateway(FakeResponse(200))

    result = gateway.send("+15551234567", "hello", [])

    assert result.success is False
    assert session.post_calls == []


def test_send_rejects_empty_recipient_and_empty_body():
    gateway, session = make_gateway(FakeResponse(200))

    assert gateway.send("  ", "hello", ["215"]).success is False
    assert gateway.send("+15551234567", "", ["215"]).success is False
    assert session.post_calls == []


def test_inbound_requires_configured_endpoint():
    gateway, _ = make_gateway(FakeResponse(200))

    with pytest.raises(SmsGatewayError):
        gateway.fetch_inbound()


def test_inbound_parses_message_list():
    body = {"messages": [{"number": "+15550000001", "message": "reply", "device": "215"}]}
    gateway, session = make_gateway(
        FakeResponse(200, json_body=body),
        inbound_endpoint="https://sms.voidfix.com/services/inbound.php",
    )

    messages = list(gateway.fetch_inbound())

    assert len(messages) == 1
    assert messages[0].from_number == "+15550000001"
    assert messages[0].message == "reply"
    assert messages[0].device_id == "215"
    assert session.get_calls[0]["params"] == {"key": SECRET}


def test_inbound_poll_success_false_raises():
    body = {"success": False, "error": "no messages endpoint"}
    gateway, _ = make_gateway(
        FakeResponse(200, json_body=body),
        inbound_endpoint="https://sms.voidfix.com/services/inbound.php",
    )

    with pytest.raises(SmsGatewayError, match="no messages endpoint"):
        gateway.fetch_inbound()


def test_parse_inbound_nested_data_and_alternate_keys():
    payload = {
        "success": True,
        "data": {
            "messages": [
                {
                    "from": "+15550000002",
                    "text": "hi",
                    "deviceID": 215,
                    "receivedAt": "2026-09-10T00:00:00Z",
                }
            ]
        },
    }

    messages = parse_inbound_payload(payload)

    assert messages[0].from_number == "+15550000002"
    assert messages[0].message == "hi"
    assert messages[0].device_id == "215"
    assert messages[0].received_at == "2026-09-10T00:00:00Z"


def test_parse_inbound_single_object_and_malformed():
    single = parse_inbound_payload({"number": "+1", "message": "x"})
    assert len(single) == 1
    with pytest.raises(SmsGatewayError):
        parse_inbound_payload({"unexpected": True})
    with pytest.raises(SmsGatewayError):
        parse_inbound_payload("not-json-object")


def test_ingest_inbound_delegates_to_parser():
    gateway, _ = make_gateway(FakeResponse(200))
    messages = list(gateway.ingest_inbound([{"number": "+1", "message": "x", "device_id": "9"}]))
    assert messages[0].device_id == "9"


def test_interpret_send_response_non_json_2xx_is_unknown():
    result = interpret_send_response(FakeResponse(202, text="accepted"), "+1555")
    assert result.success is False
    assert result.outcome is SmsSendOutcome.UNKNOWN
    assert result.provider_message_id is None
    assert result.status_code == 202
    assert "unknown" in (result.error or "")


def test_json_200_without_success_flag_is_unknown_and_keeps_id():
    result = interpret_send_response(FakeResponse(200, json_body={"id": "msg-9"}), "+1555")
    assert result.success is False
    assert result.outcome is SmsSendOutcome.UNKNOWN
    assert result.provider_message_id == "msg-9"


def test_unrecognized_success_flag_is_unknown():
    result = interpret_send_response(
        FakeResponse(200, json_body={"success": "maybe", "id": "msg-8"}),
        "+1555",
    )
    assert result.success is False
    assert result.outcome is SmsSendOutcome.UNKNOWN


def test_api_key_is_redacted_from_error_bodies(caplog):
    echoed = f"rejected key={SECRET}"
    gateway, _ = make_gateway(FakeResponse(401, text=echoed))

    with caplog.at_level("DEBUG"):
        result = gateway.send("+15551234567", "hello", ["215"])

    assert result.success is False
    assert SECRET not in (result.error or "")
    assert "<redacted>" in (result.error or "")
    assert SECRET not in caplog.text


def test_fetch_inbound_stays_disabled_without_endpoint():
    gateway, session = make_gateway(FakeResponse(200, json_body={"messages": []}))

    with pytest.raises(SmsGatewayError, match="not configured"):
        gateway.fetch_inbound()
    assert session.get_calls == []
