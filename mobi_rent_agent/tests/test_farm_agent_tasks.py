"""Farm Agent /agent/tasks/run (mocked ADB and provisioner)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_agent_tasks import (
    FarmTaskRequest,
    execute_farm_task,
    parse_farm_task_body,
)
from application.farm_task_executor import FarmTaskExecutorDeps
from domain.models import ActivationJob, ProvisioningResult
from domain.provisioning_state import ActivationVerdict
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.adb_companion import AdbCommandResult, AdbCommandRunner
from infrastructure.config import AgentConfig


class FakeRunner(AdbCommandRunner):
    def __init__(self, state: str = "device") -> None:
        super().__init__()
        self._state = state
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        self.calls.append((serial, arguments))
        if arguments == ["get-state"]:
            return AdbCommandResult(stdout=self._state, stderr="")
        if arguments == ["reboot"]:
            return AdbCommandResult(stdout="", stderr="")
        return AdbCommandResult(stdout="", stderr="")


class FakeResolver:
    def resolve(self, job: ActivationJob) -> ActivationJob:
        return job


def _config() -> AgentConfig:
    return AgentConfig(
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
        supabase_table="hardware_queue",
        hardware_agent_token="tok",
        heartbeat_interval_seconds=15.0,
        request_timeout_seconds=10.0,
        log_level="INFO",
        adb_path="adb",
        provisioning_allowed_slot_ids=(1, 2),
    )


def test_parse_rejects_unknown_task():
    with pytest.raises(ValueError, match="unsupported"):
        parse_farm_task_body(
            {"job_id": "j1", "type": "adb_shell", "farm_slot_id": 1, "payload": {}}
        )


def test_parse_rejects_forbidden_serial_in_payload():
    with pytest.raises(ValueError, match="not allowed"):
        parse_farm_task_body(
            {
                "job_id": "j1",
                "type": "assign",
                "farm_slot_id": 1,
                "payload": {"serial": "evil", "esim_qr_url": "https://x/y"},
            }
        )


def test_assign_success_when_provisioner_succeeds():
    slot_map = {1: "SERIAL-A"}
    provisioner = MagicMock()
    provisioner.provision.return_value = ProvisioningResult(
        success=True,
        job_id="job-1",
        slot_id=1,
        verdict=ActivationVerdict.ACTIVATION_CONFIRMED,
    )
    runner = FakeRunner()
    req = FarmTaskRequest(
        job_id="job-1",
        task_type="assign",
        farm_slot_id=1,
        payload={"esim_qr_url": "https://example.com/qr.png"},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(
            command_runner=runner,
            provisioner=provisioner,
            payload_resolver=FakeResolver(),
        ),
    )
    assert result.ok is True
    assert result.http_status == 200
    provisioner.provision.assert_called_once()
    assert provisioner.provision.call_args[0][0] == "SERIAL-A"


def test_assign_failed_when_provisioner_fails():
    slot_map = {1: "SERIAL-A"}
    provisioner = MagicMock()
    provisioner.provision.return_value = ProvisioningResult(
        success=False,
        job_id="job-1",
        slot_id=1,
        error="human Settings/LPA required",
    )
    req = FarmTaskRequest(
        job_id="job-1",
        task_type="assign",
        farm_slot_id=1,
        payload={"esim_qr_url": "https://example.com/qr.png"},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(
            command_runner=FakeRunner(),
            provisioner=provisioner,
            payload_resolver=FakeResolver(),
        ),
    )
    assert result.ok is False
    assert result.error == "provisioning_failed"
    assert result.http_status == 422


def test_assign_slot_isolation():
    slot_map = {1: "SERIAL-A", 2: "SERIAL-B"}
    provisioner = MagicMock()
    req = FarmTaskRequest(
        job_id="job-1",
        task_type="assign",
        farm_slot_id=2,
        payload={"esim_qr_url": "https://example.com/qr.png"},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(
            command_runner=FakeRunner(),
            provisioner=provisioner,
            payload_resolver=FakeResolver(),
            isolation=SlotIsolationPolicy(frozenset({1})),
        ),
    )
    assert result.ok is False
    assert result.error == "provisioning_failed"
    provisioner.provision.assert_not_called()


def test_assign_offline_device():
    slot_map = {1: "SERIAL-A"}
    runner = FakeRunner(state="offline")
    req = FarmTaskRequest(
        job_id="job-1",
        task_type="assign",
        farm_slot_id=1,
        payload={"esim_qr_url": "https://example.com/qr.png"},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.error == "device_offline"
    assert result.http_status == 409


def test_airplane_and_voidfix_return_501():
    slot_map = {1: "SERIAL-A"}
    for task_type in ("airplane_cycle", "voidfix_repair"):
        req = FarmTaskRequest(job_id="j", task_type=task_type, farm_slot_id=1, payload={})
        result = execute_farm_task(
            adb_path="adb",
            slot_map=slot_map,
            request=req,
            agent_config=_config(),
        )
        assert result.http_status == 501
        assert result.error == "action_not_supported"
        assert result.message


def test_reboot_uses_mapped_serial_only():
    slot_map = {1: "SERIAL-A"}
    runner = FakeRunner()
    req = FarmTaskRequest(job_id="j", task_type="reboot", farm_slot_id=1, payload={})
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.ok is True
    assert ("SERIAL-A", ["reboot"]) in runner.calls
