from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import ActivationJob
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.adb_companion import AdbCommandResult
from infrastructure.adb_provisioner import AdbCompanionProvisioner


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        self.calls.append((serial, arguments))
        if arguments == ["get-state"]:
            return AdbCommandResult(stdout="device", stderr="")
        if arguments == ["shell", "getprop", "sys.boot_completed"]:
            return AdbCommandResult(stdout="1", stderr="")
        if arguments == ["shell", "pm", "list", "features"]:
            return AdbCommandResult(stdout="feature:android.hardware.telephony.euicc", stderr="")
        if arguments[:2] == ["forward", "tcp:0"]:
            return AdbCommandResult(stdout="43210", stderr="")
        return AdbCommandResult(stdout="", stderr="")


class FakeSocket:
    def __init__(self, response: dict) -> None:
        self.response = json.dumps(response).encode() + b"\n"
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent = data

    def recv(self, _size: int) -> bytes:
        response, self.response = self.response, b""
        return response


def _queue_sockets(monkeypatch, sockets: list[FakeSocket]) -> None:
    remaining = list(sockets)

    def factory(*_args, **_kwargs):
        return remaining.pop(0)

    monkeypatch.setattr("infrastructure.adb_companion.socket.create_connection", factory)


def test_legacy_provisioner_never_sends_code_when_quarantined(monkeypatch):
    runner = FakeRunner()
    status_socket = FakeSocket(
        {
            "success": True,
            "can_silent_install": True,
            "real_esim_enabled": True,
            "euicc_enabled": True,
        }
    )
    provision_socket = FakeSocket({"success": True, "device_code": 0})
    _queue_sockets(monkeypatch, [status_socket, provision_socket])
    provisioner = AdbCompanionProvisioner(runner)
    job = ActivationJob("job-1", 1, "LPA:1$server$secret")

    result = provisioner.provision("SERIAL-1", job)

    assert result.success is False
    assert "quarantined" in (result.error or "")
    assert provision_socket.sent == b""
    assert "secret" not in (result.error or "")
    assert "activation_code" not in result.to_dict()


def test_privilege_gate_does_not_send_activation_code(monkeypatch):
    runner = FakeRunner()
    status_socket = FakeSocket(
        {
            "success": True,
            "can_silent_install": False,
            "real_esim_enabled": True,
            "has_write_embedded_subscriptions": False,
            "has_carrier_privileges": False,
        }
    )
    provision_socket = FakeSocket({"success": True, "device_code": 0})
    _queue_sockets(monkeypatch, [status_socket, provision_socket])
    provisioner = AdbCompanionProvisioner(runner)
    job = ActivationJob("job-1", 1, "LPA:1$server$secret")

    result = provisioner.provision("SERIAL-1", job)

    assert result.success is False
    assert "cannot silently install" in (result.error or "")
    assert provision_socket.sent == b""
    assert json.loads(status_socket.sent) == {"command": "get_esim_status"}
    assert "activation_code" not in json.loads(status_socket.sent)


def test_real_esim_disabled_does_not_send_activation_code(monkeypatch):
    runner = FakeRunner()
    status_socket = FakeSocket(
        {"success": True, "can_silent_install": True, "real_esim_enabled": False}
    )
    provision_socket = FakeSocket({"success": True, "device_code": 0})
    _queue_sockets(monkeypatch, [status_socket, provision_socket])
    provisioner = AdbCompanionProvisioner(runner)
    result = provisioner.provision("SERIAL-1", ActivationJob("job-1", 1, "LPA:1$server$secret"))
    assert result.success is False
    assert "REAL_ESIM_ENABLED" in (result.error or "")
    assert provision_socket.sent == b""


def test_legacy_provisioner_refuses_slot_outside_allowlist(monkeypatch):
    runner = FakeRunner()
    provision_socket = FakeSocket({"success": True, "device_code": 0})
    _queue_sockets(monkeypatch, [provision_socket])
    provisioner = AdbCompanionProvisioner(runner, isolation=SlotIsolationPolicy({1}))
    result = provisioner.provision("SERIAL-2", ActivationJob("job-2", 2, "LPA:1$server$secret"))
    assert result.success is False
    assert "allowlist" in (result.error or "")
    assert provision_socket.sent == b""
    assert runner.calls == []
