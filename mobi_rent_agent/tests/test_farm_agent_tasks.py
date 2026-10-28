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
        self._airplane_on = False
        self.calls: list[tuple[str, list[str]]] = []
        self.dumpsys_activity = ""

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        self.calls.append((serial, arguments))
        if arguments == ["get-state"]:
            return AdbCommandResult(stdout=self._state, stderr="")
        if arguments == ["reboot"]:
            return AdbCommandResult(stdout="", stderr="")
        if arguments[:3] == ["shell", "dumpsys", "activity"]:
            return AdbCommandResult(stdout=self.dumpsys_activity, stderr="")
        joined = " ".join(arguments)
        if "settings put global airplane_mode_on" in joined:
            self._airplane_on = joined.rstrip().endswith("1")
            return AdbCommandResult(stdout="", stderr="")
        if "settings get global airplane_mode_on" in joined:
            return AdbCommandResult(stdout="1" if self._airplane_on else "0", stderr="")
        if "AIRPLANE_MODE" in joined:
            if "true" in joined.split("state")[-1]:
                self._airplane_on = True
            elif "false" in joined.split("state")[-1]:
                self._airplane_on = False
            return AdbCommandResult(stdout="Broadcast completed: result=0", stderr="")
        if arguments and arguments[0] == "shell" and "airplane-mode" in joined:
            if arguments[-1].endswith("enable") or "enable" in joined:
                self._airplane_on = True
                return AdbCommandResult(stdout="enabled", stderr="")
            if arguments[-1].endswith("disable") or "disable" in joined:
                self._airplane_on = False
                return AdbCommandResult(stdout="disabled", stderr="")
            return AdbCommandResult(stdout="enabled" if self._airplane_on else "disabled", stderr="")
        return AdbCommandResult(stdout="", stderr="")

    def run_bytes(self, serial: str, arguments: list[str]) -> bytes:
        self.calls.append((serial, arguments))
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
            b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
        )


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


def test_parse_accepts_slot_21():
    request = parse_farm_task_body(
        {"job_id": "j21", "type": "device_display_size", "farm_slot_id": 21, "payload": {}}
    )
    assert request.farm_slot_id == 21


def test_parse_rejects_slot_above_max():
    with pytest.raises(ValueError, match="out of range"):
        parse_farm_task_body(
            {"job_id": "j33", "type": "device_display_size", "farm_slot_id": 33, "payload": {}}
        )


def test_parse_rejects_unknown_task():
    with pytest.raises(ValueError, match="unsupported"):
        parse_farm_task_body(
            {"job_id": "j1", "type": "adb_shell", "farm_slot_id": 1, "payload": {}}
        )


def test_parse_cleanup_accepts_optional_serial():
    request = parse_farm_task_body(
        {
            "job_id": "j21",
            "type": "device_data_cleanup",
            "farm_slot_id": 21,
            "payload": {"serial": "RFCNB056WEV", "lockdown": True},
        }
    )
    assert request.payload["serial"] == "RFCNB056WEV"


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


def test_assign_never_calls_automatic_esim_provision():
    slot_map = {1: "SERIAL-A"}
    provisioner = MagicMock()
    provisioner.provision.return_value = ProvisioningResult(
        success=True,
        job_id="job-1",
        slot_id=1,
        verdict=ActivationVerdict.ACTIVATION_CONFIRMED,
    )
    resolver = FakeResolver()
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
            payload_resolver=resolver,
        ),
    )
    assert result.ok is False
    assert result.http_status == 422
    assert result.error == "provisioning_failed"
    assert "human Settings/LPA required" in (result.message or "")
    provisioner.provision.assert_not_called()


def test_assign_failed_when_provisioner_would_fail():
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
    provisioner.provision.assert_not_called()


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


def test_airplane_mode_runs_and_voidfix_needs_package():
    slot_map = {1: "SERIAL-A"}
    runner = FakeRunner()
    air = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=FarmTaskRequest(
            job_id="j",
            task_type="airplane_mode",
            farm_slot_id=1,
            payload={"state": "off"},
        ),
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert air.ok is True
    assert air.http_status == 200
    void = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=FarmTaskRequest(job_id="j", task_type="voidfix_repair", farm_slot_id=1, payload={}),
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert void.http_status == 422
    assert void.error == "invalid_request"


def test_setup_session_inspect_uses_mapped_serial():
    slot_map = {1: "SERIAL-A"}
    runner = FakeRunner()
    runner.dumpsys_activity = (
        "mResumedActivity: ActivityRecord{abc u0 com.android.settings/.network.telephony.MobileNetworkActivity t1}"
    )
    req = FarmTaskRequest(
        job_id="j",
        task_type="setup_session_inspect",
        farm_slot_id=1,
        payload={"phase": "esim", "recover": False},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.ok is True
    assert result.details["allowed"] is True
    assert any(call[1][:2] == ["shell", "dumpsys"] for call in runner.calls)
    inspect_joined = " ".join(" ".join(call[1]) for call in runner.calls)
    assert "uiautomator" not in inspect_joined
    assert "input tap" not in inspect_joined
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


def test_setup_session_inspect_reasserts_silent_and_leaves_sound_settings():
    slot_map = {21: "RFCNB056WEV"}
    runner = FakeRunner()
    runner.dumpsys_activity = (
        "mResumedActivity: ActivityRecord{abc u0 "
        "com.android.settings/.Settings$SecSoundSettingsActivity t1}"
    )
    req = FarmTaskRequest(
        job_id="j",
        task_type="setup_session_inspect",
        farm_slot_id=21,
        payload={"phase": "esim", "recover": False},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.ok is True
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert "mode_ringer 0" in joined
    assert "vibrate_on 0" in joined
    assert "input keyevent 3" in joined
    assert "force-stop com.android.settings" in joined


def test_setup_session_inspect_recover_uses_samsung_sim_manager():
    from application.setup_activity_guard import SAMSUNG_SIM_MGR_COMPONENT

    slot_map = {21: "RFCNB056WEV"}
    runner = FakeRunner()
    runner.dumpsys_activity = (
        "mResumedActivity: ActivityRecord{abc u0 com.android.settings/.homepage.SettingsHomepageActivity t1}"
    )
    req = FarmTaskRequest(
        job_id="j",
        task_type="setup_session_inspect",
        farm_slot_id=21,
        payload={"phase": "esim", "recover": True},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.ok is True
    starts = [args for _serial, args in runner.calls if args[:3] == ["shell", "am", "start"]]
    assert starts
    joined = " ".join(starts[0])
    assert SAMSUNG_SIM_MGR_COMPONENT in joined
    assert "MANAGE_ALL_SIM_PROFILES_SETTINGS" not in joined
    assert "SIM_CARD_SETTINGS" not in joined


def test_setup_session_inspect_kills_game_launcher_and_opens_sim_manager():
    from application.setup_activity_guard import SAMSUNG_SIM_MGR_COMPONENT

    slot_map = {21: "RFCNB056WEV"}
    runner = FakeRunner()
    runner.dumpsys_activity = (
        "mResumedActivity: ActivityRecord{abc u0 com.samsung.android.game.gamehome/.MainActivity t1}"
    )
    req = FarmTaskRequest(
        job_id="j",
        task_type="setup_session_inspect",
        farm_slot_id=21,
        payload={"phase": "esim", "recover": True},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.ok is True
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert "am force-stop com.samsung.android.game.gamehome" in joined
    assert "KEYCODE_HOME" in joined
    assert SAMSUNG_SIM_MGR_COMPONENT in joined
    assert "android.settings.SETTINGS" not in joined
    assert "MANAGE_ALL_SIM_PROFILES_SETTINGS" not in joined


def test_setup_session_input_settings_uses_explicit_component_on_samsung():
    slot_map = {21: "RFCNB056WEV"}
    runner = FakeRunner()
    req = FarmTaskRequest(
        job_id="j",
        task_type="setup_session_input",
        farm_slot_id=21,
        payload={"kind": "settings"},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.ok is True
    starts = [args for _serial, args in runner.calls if args[:3] == ["shell", "am", "start"]]
    assert starts
    joined = " ".join(starts[0])
    assert "com.android.settings/.Settings" in joined
    assert "-n" in starts[0]
    assert "android.settings.SETTINGS" not in joined


def test_setup_session_inspect_skips_recover_after_qr_walk():
    from application.setup_activity_guard import SAMSUNG_SIM_MGR_COMPONENT

    slot_map = {21: "RFCNB056WEV"}
    runner = FakeRunner()
    runner.dumpsys_activity = (
        "mResumedActivity: ActivityRecord{abc u0 com.android.settings/.spa.SpaActivity t1}"
    )
    req = FarmTaskRequest(
        job_id="j",
        task_type="setup_session_inspect",
        farm_slot_id=21,
        payload={"phase": "esim", "recover": True, "qr_walk_completed": True},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.ok is True
    assert result.details["recovered"] is False
    starts = [args for _serial, args in runner.calls if args[:3] == ["shell", "am", "start"]]
    assert starts == []
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert SAMSUNG_SIM_MGR_COMPONENT not in joined


def test_setup_session_input_scales_normalized_tap_to_wm_size():
    slot_map = {21: "RFCNB056WEV"}
    runner = FakeRunner()

    class SizeRunner(FakeRunner):
        def run(self, serial: str, arguments: list[str]):
            self.calls.append((serial, arguments))
            if arguments[:3] == ["shell", "wm", "size"]:
                return AdbCommandResult(
                    stdout="Physical size: 1440x3200\nOverride size: 1080x2400\n",
                    stderr="",
                )
            return AdbCommandResult(stdout="device" if arguments == ["get-state"] else "", stderr="")

    sized = SizeRunner()
    req = FarmTaskRequest(
        job_id="j",
        task_type="setup_session_input",
        farm_slot_id=21,
        payload={"kind": "tap", "x": 0.5, "y": 0.5},
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map=slot_map,
        request=req,
        deps=FarmTaskExecutorDeps(command_runner=sized),
    )
    assert result.ok is True
    taps = [args for _s, args in sized.calls if args[:3] == ["shell", "input", "tap"]]
    assert taps == [["shell", "input", "tap", "540", "1200"]]
