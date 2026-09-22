from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.verification import (
    ConnectivityLayer,
    FourLayerVerification,
    SettingsLpaLayer,
    SubscriptionLayer,
    TelephonyLayer,
)
from tools.prototype_readiness import _load_service, main


def test_cli_requires_prototype_env():
    assert main(["--env", "production", "audit"]) == 2


def test_cli_audit_with_example_map(capsys):
    devices = Path(__file__).resolve().parents[1] / "config" / "prototype" / "prototype_devices.example.json"
    code = main(
        [
            "--env",
            "prototype",
            "--devices",
            str(devices),
            "--prototype-env",
            str(Path("does-not-exist.env")),
            "--slot-map",
            str(Path("missing-slot-map.json")),
            "audit",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["environment"] == "prototype"
    assert payload["sms_sent"] is False
    assert payload["voidfix_enabled"] is False


def test_cli_wires_prototype_json_client_from_env_key(tmp_path: Path, monkeypatch):
    devices = tmp_path / "devices.json"
    devices.write_text(
        json.dumps(
            {
                "prototype-device-1": {
                    "device_type": "pixel_7a",
                    "role": "test_pixel_7a",
                    "adb_serial": "TESTSERIAL7A",
                    "voidfix_device_id": "1385",
                    "enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROTOTYPE_ENVIRONMENT", "prototype")
    monkeypatch.setenv("VOIDFIX_ENABLED", "true")
    monkeypatch.setenv("VOIDFIX_API_KEY", "wired-key-from-env")
    constructed: dict = {}

    def fake_client(api_key, **kwargs):
        constructed["api_key"] = api_key
        constructed["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("tools.prototype_readiness.PrototypeVoidFixRestClient", fake_client)
    args = argparse.Namespace(
        env="prototype",
        prototype_env=str(tmp_path / "missing.env"),
        devices=str(devices),
        slot_map=str(tmp_path / "missing-slot-map.json"),
        command="send-test-sms",
    )
    service = _load_service(args)
    assert constructed["api_key"] == "wired-key-from-env"
    assert service._gateway is not None
    readiness = Path(__file__).resolve().parents[1] / "tools" / "prototype_readiness.py"
    text = readiness.read_text(encoding="utf-8")
    assert "VoidFixSmsGateway" not in text
    assert "PrototypeVoidFixRestClient" in text
    assert "live_android_sim_slot_index" in text


def test_cli_send_passes_live_android_slot_and_does_not_post(tmp_path: Path, monkeypatch):
    devices = tmp_path / "devices.json"
    devices.write_text(
        json.dumps(
            {
                "prototype-device-1": {
                    "device_type": "pixel_7a",
                    "role": "test_pixel_7a",
                    "adb_serial": "TESTSERIAL7A",
                    "voidfix_device_id": "1385",
                    "enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )
    captured: dict = {}

    def fake_send(self, device_id, recipient, **kwargs):
        captured["android_sim_slot_index"] = kwargs.get("android_sim_slot_index")
        return {"sms_sent": False, "status": "refused"}

    verification = FourLayerVerification(
        settings_lpa=SettingsLpaLayer(),
        subscription=SubscriptionLayer(sim_slot_index=1),
        telephony=TelephonyLayer(),
        connectivity=ConnectivityLayer(),
    )
    monkeypatch.setattr("tools.prototype_readiness._live_probe", lambda *_args: (None, "", verification))
    monkeypatch.setattr("application.prototype_service.PrototypeService.send_test_sms", fake_send)
    code = main(
        [
            "--env",
            "prototype",
            "--devices",
            str(devices),
            "--prototype-env",
            str(tmp_path / "missing.env"),
            "--slot-map",
            str(tmp_path / "missing-slot-map.json"),
            "--recipient",
            "+15555550100",
            "send-test-sms",
        ]
    )
    assert captured["android_sim_slot_index"] == 1
    assert code == 2
