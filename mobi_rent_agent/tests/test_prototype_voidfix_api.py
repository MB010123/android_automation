from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import SmsSendOutcome
from domain.ports import SmsGateway
from domain.prototype import PROTOTYPE_DEVICE_ID, PROTOTYPE_VOIDFIX_DEVICE_ID
from domain.verification import (
    ConnectivityLayer,
    FourLayerVerification,
    SettingsLpaLayer,
    SubscriptionLayer,
    TelephonyLayer,
)
from infrastructure.prototype_voidfix_api import (
    FORBIDDEN_REQUEST_FIELDS,
    HARMLESS_TEST_SMS,
    JSON_CONTENT_TYPE,
    MISSING_LIVE_SLOT_REASON,
    REQUIRED_API_SLOT,
    SEND_PHP_HAS_CONFIRMED_SLOT_PARAMETER,
    SLOT_ZERO_REFUSED_REASON,
    PrototypeVoidFixRestClient,
    assert_slot_parameter_selects_sim2,
    live_android_sim_slot_index,
    voidfix_slot_from_live_android,
)
from infrastructure.voidfix_api import SmsGatewayError, VoidFixSmsGateway

SECRET = "voidfix-prototype-test-secret-do-not-log"
RECIPIENT = "+15555550100"
AGENT_ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body=None, text: str = "") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.post_calls: list[dict] = []

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.post_calls.append(
            {
                "url": url,
                "data": data,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self.response


def make_client(response: FakeResponse | None = None, **kwargs) -> tuple[PrototypeVoidFixRestClient, FakeSession]:
    session = FakeSession(response or FakeResponse(200, json_body={"success": True, "id": "msg-1"}))
    client = PrototypeVoidFixRestClient(
        api_key=kwargs.pop("api_key", SECRET),
        session=session,
        **kwargs,
    )
    return client, session


def test_confirmed_slot_parameter_maps_sim2():
    assert SEND_PHP_HAS_CONFIRMED_SLOT_PARAMETER is True
    assert REQUIRED_API_SLOT == 1
    assert_slot_parameter_selects_sim2()
    assert voidfix_slot_from_live_android(1) == 1


def test_client_implements_sms_gateway_port():
    assert issubclass(PrototypeVoidFixRestClient, SmsGateway)


def test_empty_api_key_is_rejected():
    with pytest.raises(SmsGatewayError):
        PrototypeVoidFixRestClient(api_key="  ")


def test_send_endpoint_must_be_https():
    with pytest.raises(SmsGatewayError):
        PrototypeVoidFixRestClient(api_key="k", send_endpoint="http://insecure.example/send.php")


def test_json_payload_uses_confirmed_slot_contract(monkeypatch):
    monkeypatch.setenv("VOIDFIX_API_KEY", "env-key-must-not-be-read-by-client")
    client, _session = make_client(api_key=SECRET)
    payload = client.build_json_payload(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)

    assert payload == {
        "key": SECRET,
        "number": RECIPIENT,
        "message": HARMLESS_TEST_SMS,
        "devices": [1385],
        "slot": 1,
    }
    assert payload["devices"] == [PROTOTYPE_VOIDFIX_DEVICE_ID]
    assert isinstance(payload["devices"][0], int)
    assert isinstance(payload["slot"], int)
    for field in FORBIDDEN_REQUEST_FIELDS:
        assert field not in payload
    serialized = json.dumps(payload)
    assert "1385|1" not in serialized
    assert "[1385, 1]" not in serialized
    assert "[1385,1]" not in serialized
    assert "devices=1385" not in serialized
    assert "env-key-must-not-be-read-by-client" not in serialized


def test_execute_documented_send_posts_json_with_slot():
    client, session = make_client(FakeResponse(200, json_body={"success": True, "id": "vf-1"}))

    result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)

    assert result.success is True
    assert result.outcome is SmsSendOutcome.SUCCESS
    assert result.provider_message_id == "vf-1"
    assert len(session.post_calls) == 1
    sent = session.post_calls[0]
    assert sent["url"] == "https://sms.voidfix.com/services/send.php"
    assert sent["data"] is None
    assert sent["headers"]["Content-Type"] == JSON_CONTENT_TYPE
    assert sent["json"] == {
        "key": SECRET,
        "number": RECIPIENT,
        "message": HARMLESS_TEST_SMS,
        "devices": [1385],
        "slot": 1,
    }


def test_send_without_live_slot_does_not_post():
    client, session = make_client()

    result = client.send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"])

    assert result.success is False
    assert result.outcome is SmsSendOutcome.FAILURE
    assert result.error == MISSING_LIVE_SLOT_REASON
    assert session.post_calls == []


def test_rejects_missing_slot_zero_and_other_slots():
    client, session = make_client()
    missing = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=None)
    zero = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=0)
    other = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=2)
    assert missing.success is False
    assert missing.error == MISSING_LIVE_SLOT_REASON
    assert zero.success is False
    assert zero.error == SLOT_ZERO_REFUSED_REASON
    assert other.success is False
    assert "simSlotIndex is 2" in (other.error or "")
    assert session.post_calls == []


def test_rejects_undocumented_device_encodings():
    client, session = make_client()
    for devices in (["1385|1"], ["[1385,1]"], ["1385,1"], ["1385", "1"], ["1"], ["215"]):
        result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, devices, slot=1)
        assert result.success is False
        assert session.post_calls == []


def test_api_key_comes_from_constructor_and_is_redacted(monkeypatch, caplog):
    monkeypatch.setenv("VOIDFIX_API_KEY", "other-env-secret")
    echoed = f"rejected key={SECRET}"
    client, session = make_client(FakeResponse(401, text=echoed), api_key=SECRET)

    with caplog.at_level("DEBUG"):
        result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)

    assert result.success is False
    assert result.outcome is SmsSendOutcome.FAILURE
    assert result.status_code == 401
    assert SECRET not in (result.error or "")
    assert "<redacted>" in (result.error or "")
    assert SECRET not in caplog.text
    assert session.post_calls[0]["json"]["key"] == SECRET
    assert session.post_calls[0]["json"]["key"] != "other-env-secret"


def test_documented_success_response():
    body = {"success": True, "data": {"messages": [{"ID": 77, "status": "Pending"}]}}
    client, _ = make_client(FakeResponse(200, json_body=body))
    result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)
    assert result.success is True
    assert result.outcome is SmsSendOutcome.SUCCESS
    assert result.provider_message_id == "77"


def test_explicit_provider_failure():
    body = {"success": False, "error": {"code": 401, "message": "Invalid API key."}}
    client, _ = make_client(FakeResponse(200, json_body=body))
    result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)
    assert result.success is False
    assert result.outcome is SmsSendOutcome.FAILURE
    assert "Invalid API key" in (result.error or "")
    assert SECRET not in (result.error or "")


def test_http_200_missing_success_is_unknown():
    client, _ = make_client(FakeResponse(200, json_body={"id": "msg-9"}))
    result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)
    assert result.success is False
    assert result.outcome is SmsSendOutcome.UNKNOWN
    assert result.provider_message_id == "msg-9"
    assert result.success is not True


def test_malformed_json_http_200_is_unknown():
    client, _ = make_client(FakeResponse(200, text="not-json"))
    result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)
    assert result.success is False
    assert result.outcome is SmsSendOutcome.UNKNOWN
    assert "unknown" in (result.error or "")


def test_http_4xx_is_failure():
    client, _ = make_client(FakeResponse(400, text="bad request"))
    result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)
    assert result.success is False
    assert result.outcome is SmsSendOutcome.FAILURE
    assert result.status_code == 400


def test_http_5xx_is_failure():
    client, _ = make_client(FakeResponse(503, text="unavailable"))
    result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)
    assert result.success is False
    assert result.outcome is SmsSendOutcome.FAILURE
    assert result.status_code == 503


def test_timeout_is_failure_without_retry():
    client, session = make_client()
    calls = {"count": 0}

    def boom(*_args, **_kwargs):
        calls["count"] += 1
        raise requests.exceptions.Timeout("timed out")

    session.post = boom  # type: ignore[method-assign]
    result = client.execute_documented_send(RECIPIENT, HARMLESS_TEST_SMS, ["1385"], slot=1)
    assert result.success is False
    assert "timeout" in (result.error or "")
    assert SECRET not in (result.error or "")
    assert calls["count"] == 1


def test_live_android_slot_is_read_not_hardcoded():
    missing = FourLayerVerification(
        settings_lpa=SettingsLpaLayer(),
        subscription=SubscriptionLayer(sim_slot_index=None),
        telephony=TelephonyLayer(),
        connectivity=ConnectivityLayer(),
    )
    live = FourLayerVerification(
        settings_lpa=SettingsLpaLayer(),
        subscription=SubscriptionLayer(sim_slot_index=1),
        telephony=TelephonyLayer(),
        connectivity=ConnectivityLayer(),
    )
    assert live_android_sim_slot_index(None) is None
    assert live_android_sim_slot_index(missing) is None
    assert live_android_sim_slot_index(live) == 1
    with pytest.raises(SmsGatewayError, match="required"):
        voidfix_slot_from_live_android(live_android_sim_slot_index(None))


def test_production_gateway_still_posts_form_urlencoded_without_slot():
    session = FakeSession(FakeResponse(200, json_body={"success": True}))
    gateway = VoidFixSmsGateway(api_key=SECRET, session=session)
    gateway.send(RECIPIENT, "hello", ["215"])
    sent = session.post_calls[0]
    assert sent["data"] == {
        "number": RECIPIENT,
        "devices": "215",
        "key": SECRET,
        "message": "hello",
    }
    assert "slot" not in sent["data"]
    assert sent["json"] is None


def test_production_sms_modules_do_not_use_prototype_slot_client():
    api = (AGENT_ROOT / "infrastructure" / "voidfix_api.py").read_text(encoding="utf-8")
    sms = (AGENT_ROOT / "application" / "sms_service.py").read_text(encoding="utf-8")
    assert "prototype_voidfix" not in api
    assert "prototype_voidfix" not in sms
    assert '"slot"' not in api
    assert "send_for_slot" in sms


def test_prototype_modules_never_call_farm_or_mutate_device():
    paths = [
        AGENT_ROOT / "application" / "prototype_service.py",
        AGENT_ROOT / "infrastructure" / "prototype_voidfix_api.py",
        AGENT_ROOT / "tools" / "prototype_readiness.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "from application.sms_service" not in text
        assert "SmsDispatchService" not in text
        assert ".send_for_slot(" not in text
        assert "forceDeactivateSim" not in text
        assert "deleteSubscription" not in text
        assert "persist.radio" not in text
        assert "application/x-www-form-urlencoded" not in text
        assert ".reboot(" not in text


def test_execute_documented_send_has_no_retry_loop():
    source = inspect.getsource(PrototypeVoidFixRestClient.execute_documented_send)
    assert "while True" not in source
    assert source.count("self._session.post") == 1


def test_allowed_ids_are_prototype_device_one_and_1385():
    assert PROTOTYPE_DEVICE_ID == "prototype-device-1"
    assert PROTOTYPE_VOIDFIX_DEVICE_ID == 1385
