"""Unit tests for infrastructure.adb_slot_status.

`subprocess.run` is monkeypatched so these tests exercise the real
`adb devices` output parsing and online/networkerror decision logic
without needing an attached device - the same parsing that will run
against a real Pixel 6a's `adb devices -l` output.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import SlotStatus
from infrastructure.adb_slot_status import AdbSlotStatusProvider, SlotMapError, load_slot_map


class FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_load_slot_map_missing_file_raises(tmp_path):
    with pytest.raises(SlotMapError):
        load_slot_map(tmp_path / "does_not_exist.json")


def test_load_slot_map_invalid_json_raises(tmp_path):
    path = tmp_path / "slot_map.json"
    path.write_text("{not valid json")

    with pytest.raises(SlotMapError):
        load_slot_map(path)


def test_load_slot_map_non_integer_key_raises(tmp_path):
    path = tmp_path / "slot_map.json"
    path.write_text(json.dumps({"one": "SERIAL1"}))

    with pytest.raises(SlotMapError):
        load_slot_map(path)


def test_load_slot_map_parses_valid_mapping(tmp_path):
    path = tmp_path / "slot_map.json"
    path.write_text(json.dumps({"1": "SERIAL1", "2": "SERIAL2"}))

    slot_map = load_slot_map(path)

    assert slot_map == {1: "SERIAL1", 2: "SERIAL2"}


def _patch_adb(monkeypatch, devices_output: str, probe_ok_serials: set[str]):
    def fake_run(command, capture_output, text, timeout, check):
        assert command[0] == "adb"
        if command[1:] == ["devices"]:
            return FakeCompletedProcess(stdout=devices_output)
        # ["-s", serial, "shell", "echo", "ok"]
        serial = command[2]
        if serial in probe_ok_serials:
            return FakeCompletedProcess(stdout="ok\n", returncode=0)
        return FakeCompletedProcess(stdout="", returncode=1)

    monkeypatch.setattr("infrastructure.adb_slot_status.subprocess.run", fake_run)


def test_online_when_device_present_and_probe_succeeds(monkeypatch):
    _patch_adb(
        monkeypatch,
        devices_output="List of devices attached\nSERIAL1\tdevice\n",
        probe_ok_serials={"SERIAL1"},
    )
    provider = AdbSlotStatusProvider(slot_map={1: "SERIAL1"})

    states = list(provider.read_slot_states())

    assert states[0].slot_id == 1
    assert states[0].status == SlotStatus.ONLINE


def test_networkerror_when_serial_missing_from_adb_devices(monkeypatch):
    _patch_adb(monkeypatch, devices_output="List of devices attached\n", probe_ok_serials=set())
    provider = AdbSlotStatusProvider(slot_map={1: "SERIAL1"})

    states = list(provider.read_slot_states())

    assert states[0].status == SlotStatus.NETWORK_ERROR


@pytest.mark.parametrize("adb_state", ["unauthorized", "offline"])
def test_networkerror_for_unauthorized_or_offline_state(monkeypatch, adb_state):
    _patch_adb(
        monkeypatch,
        devices_output=f"List of devices attached\nSERIAL1\t{adb_state}\n",
        probe_ok_serials={"SERIAL1"},
    )
    provider = AdbSlotStatusProvider(slot_map={1: "SERIAL1"})

    states = list(provider.read_slot_states())

    assert states[0].status == SlotStatus.NETWORK_ERROR


def test_networkerror_when_device_listed_but_shell_probe_fails(monkeypatch):
    _patch_adb(
        monkeypatch,
        devices_output="List of devices attached\nSERIAL1\tdevice\n",
        probe_ok_serials=set(),
    )
    provider = AdbSlotStatusProvider(slot_map={1: "SERIAL1"})

    states = list(provider.read_slot_states())

    assert states[0].status == SlotStatus.NETWORK_ERROR


def test_all_slots_networkerror_when_adb_binary_is_missing(monkeypatch):
    def raise_not_found(*_args, **_kwargs):
        raise FileNotFoundError("adb not on PATH")

    monkeypatch.setattr("infrastructure.adb_slot_status.subprocess.run", raise_not_found)
    provider = AdbSlotStatusProvider(slot_map={1: "SERIAL1", 2: "SERIAL2"})

    states = list(provider.read_slot_states())

    assert [s.status for s in states] == [SlotStatus.NETWORK_ERROR, SlotStatus.NETWORK_ERROR]


def test_probe_timeout_is_treated_as_networkerror_not_a_crash(monkeypatch):
    devices_output = "List of devices attached\nSERIAL1\tdevice\n"

    def fake_run(command, capture_output, text, timeout, check):
        if command[1:] == ["devices"]:
            return FakeCompletedProcess(stdout=devices_output)
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    monkeypatch.setattr("infrastructure.adb_slot_status.subprocess.run", fake_run)
    provider = AdbSlotStatusProvider(slot_map={1: "SERIAL1"})

    states = list(provider.read_slot_states())

    assert states[0].status == SlotStatus.NETWORK_ERROR
