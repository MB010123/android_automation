from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import ActivationJob
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


def test_activation_code_uses_forwarded_socket_and_forward_is_removed(monkeypatch):
    runner = FakeRunner()
    fake_socket = FakeSocket({"success": True, "device_code": 0})
    monkeypatch.setattr(
        "infrastructure.adb_companion.socket.create_connection",
        lambda *_args, **_kwargs: fake_socket,
    )
    provisioner = AdbCompanionProvisioner(runner)
    job = ActivationJob("job-1", 1, "LPA:1$server$secret")

    result = provisioner.provision("SERIAL-1", job)

    assert result.success is True
    assert json.loads(fake_socket.sent) == {
        "command": "provision_esim",
        "job_id": "job-1",
        "slot_id": 1,
        "activation_code": "LPA:1$server$secret",
        "switch_after_download": True,
    }
    assert runner.calls == [
        ("SERIAL-1", ["get-state"]),
        ("SERIAL-1", ["shell", "getprop", "sys.boot_completed"]),
        ("SERIAL-1", ["shell", "pm", "list", "features"]),
        ("SERIAL-1", ["forward", "tcp:0", "localabstract:mobi_rent.provisioning"]),
        ("SERIAL-1", ["forward", "--remove", "tcp:43210"]),
    ]
