"""Controlled Farm Agent task execution (assign, reboot, device actions)."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.install_state import INSTALL_FAILED
from domain.models import ActivationJob
from domain.ports import ActivationPayloadResolver, SubscriptionProvisioner
from domain.slot_isolation import SlotIsolationError, SlotIsolationPolicy
from infrastructure.esim_qr_security import esim_fetch_url_is_public_https
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner
from infrastructure.adb_health import AdbDeviceHealthController
from infrastructure.config import AgentConfig

logger = logging.getLogger("farm_agent.task_executor")

FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "serial",
        "adb_serial",
        "device_serial",
        "voidfix_device_id",
        "command",
        "shell",
        "adb_command",
    }
)

AIRPLANE_UNSUPPORTED_REASON = (
    "no approved airplane-mode control in mobi_rent.network companion or device helpers; "
    "raw adb shell settings are not exposed via this API"
)
VOIDFIX_REPAIR_UNSUPPORTED_REASON = (
    "VoidFixSmsGateway has send/inbound only; no repair or reconnect API exists in this repository"
)


@dataclass(frozen=True)
class FarmTaskExecutorDeps:
    """Optional overrides for unit tests (mock ADB / provisioner)."""

    command_runner: AdbCommandRunner | None = None
    provisioner: SubscriptionProvisioner | None = None
    payload_resolver: ActivationPayloadResolver | None = None
    isolation: SlotIsolationPolicy | None = None


def execute_controlled_farm_task(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    agent_config: AgentConfig | None = None,
    deps: FarmTaskExecutorDeps | None = None,
) -> FarmTaskResult:
    deps = deps or FarmTaskExecutorDeps()
    reject_forbidden_payload_keys(request.payload)

    if request.task_type == "reboot":
        return _run_reboot(adb_path, slot_map, request, deps.command_runner)
    if request.task_type == "assign":
        if agent_config is None:
            return FarmTaskResult(
                ok=False,
                http_status=503,
                error="agent_not_configured",
                message="Farm Agent config required for assign",
            )
        return _run_assign(
            agent_config,
            slot_map,
            request,
            deps,
        )
    if request.task_type == "airplane_cycle":
        return FarmTaskResult(
            ok=False,
            http_status=501,
            error="action_not_supported",
            message=AIRPLANE_UNSUPPORTED_REASON,
        )
    if request.task_type == "voidfix_repair":
        return FarmTaskResult(
            ok=False,
            http_status=501,
            error="action_not_supported",
            message=VOIDFIX_REPAIR_UNSUPPORTED_REASON,
        )
    if request.task_type == "remote_access_place_qr":
        from application.remote_access_farm_task import run_remote_access_place_qr

        return run_remote_access_place_qr(
            adb_path=adb_path,
            slot_map=slot_map,
            request=request,
            agent_config=agent_config,
            command_runner=deps.command_runner,
        )
    if request.task_type == "remote_access_activation_status":
        from application.remote_access_farm_task import run_remote_access_activation_status

        return run_remote_access_activation_status(
            adb_path=adb_path,
            slot_map=slot_map,
            request=request,
            agent_config=agent_config,
            command_runner=deps.command_runner,
        )
    if request.task_type == "setup_session_inspect":
        from application.setup_session_farm_task import run_setup_session_inspect

        return run_setup_session_inspect(
            adb_path=adb_path,
            slot_map=slot_map,
            request=request,
            command_runner=deps.command_runner,
        )
    if request.task_type == "setup_session_input":
        from application.setup_session_farm_task import run_setup_session_input

        return run_setup_session_input(
            adb_path=adb_path,
            slot_map=slot_map,
            request=request,
            command_runner=deps.command_runner,
        )
    if request.task_type == "setup_session_voidfix_cycle":
        from application.setup_session_farm_task import run_setup_session_voidfix_cycle

        return run_setup_session_voidfix_cycle(
            adb_path=adb_path,
            slot_map=slot_map,
            request=request,
            command_runner=deps.command_runner,
        )
    if request.task_type == "setup_session_safe_cleanup":
        from application.setup_session_farm_task import run_setup_session_safe_cleanup

        return run_setup_session_safe_cleanup(
            adb_path=adb_path,
            slot_map=slot_map,
            request=request,
            command_runner=deps.command_runner,
        )
    return FarmTaskResult(ok=False, http_status=501, error="action_not_supported")


def reject_forbidden_payload_keys(payload: dict[str, Any]) -> None:
    for key in payload:
        if key in FORBIDDEN_PAYLOAD_KEYS:
            raise ValueError(f"payload key {key!r} is not allowed")


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = slot_map.get(int(farm_slot_id))
    if not serial or not str(serial).strip():
        return None
    return str(serial).strip()


def _run_reboot(
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    runner: AdbCommandRunner | None,
) -> FarmTaskResult:
    serial = _serial_for_slot(slot_map, request.farm_slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    try:
        if runner is not None:
            runner.run(serial, ["reboot"])
        else:
            subprocess.run(
                [adb_path, "-s", serial, "reboot"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except (AdbCommandError, OSError, subprocess.TimeoutExpired):
        logger.warning("farm_task_reboot_failed slot=%s", request.farm_slot_id)
        return FarmTaskResult(ok=False, http_status=422, error="reboot_failed")
    logger.info("farm_task_reboot_ok slot=%s job_id=%s", request.farm_slot_id, request.job_id)
    return FarmTaskResult(ok=True, http_status=200)


def _run_assign(
    config: AgentConfig,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    deps: FarmTaskExecutorDeps,
) -> FarmTaskResult:
    slot_id = int(request.farm_slot_id)
    serial = _serial_for_slot(slot_map, slot_id)
    if not serial:
        return FarmTaskResult(
            ok=False,
            http_status=404,
            error="slot_not_found",
            install_state=INSTALL_FAILED,
            activation_code_sent=False,
        )

    isolation = deps.isolation or SlotIsolationPolicy(config.provisioning_allowed_slot_ids)

    try:
        isolation.refuse_if_empty({slot_id: serial})
    except SlotIsolationError as exc:
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="provisioning_failed",
            message=str(exc),
            install_state=INSTALL_FAILED,
            activation_code_sent=False,
        )

    if not isolation.allows(slot_id):
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="provisioning_failed",
            message=(
                f"slot {slot_id} is outside the provisioning allowlist "
                f"{sorted(isolation.allowed_slot_ids)}"
            ),
            install_state=INSTALL_FAILED,
            activation_code_sent=False,
        )

    qr_url = str(request.payload.get("esim_qr_url") or "").strip()
    if not qr_url:
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="invalid_assignment",
            message="esim_qr_url is required",
            install_state=INSTALL_FAILED,
            activation_code_sent=False,
        )
    if not esim_fetch_url_is_public_https(qr_url):
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="invalid_assignment",
            message="esim_qr_url is not a public HTTPS URL",
            install_state=INSTALL_FAILED,
            activation_code_sent=False,
        )

    runner = deps.command_runner or AdbCommandRunner(
        config.adb_path,
        config.provisioning_timeout_seconds,
    )
    try:
        state = runner.run(serial, ["get-state"]).stdout
        if state != "device":
            return FarmTaskResult(
                ok=False,
                http_status=409,
                error="device_offline",
                message=f"ADB state is {state or 'unknown'}",
                install_state=INSTALL_FAILED,
                activation_code_sent=False,
            )
    except AdbCommandError as exc:
        return FarmTaskResult(
            ok=False,
            http_status=409,
            error="device_offline",
            message=str(exc),
            install_state=INSTALL_FAILED,
            activation_code_sent=False,
        )

    try:
        ActivationJob(job_id=request.job_id, slot_id=slot_id, qr_url=qr_url)
    except ValueError as exc:
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="invalid_assignment",
            message=str(exc),
            install_state=INSTALL_FAILED,
            activation_code_sent=False,
        )

    logger.info(
        "farm_task_assign_manual_only slot=%s job_id=%s",
        slot_id,
        request.job_id,
    )
    return FarmTaskResult(
        ok=False,
        http_status=422,
        error="provisioning_failed",
        message="human Settings/LPA required; automatic eSIM provision is disabled",
        install_state=INSTALL_FAILED,
        activation_code_sent=False,
    )


def run_airplane_cycle_if_supported(
    *,
    slot_map: dict[int, str],
    farm_slot_id: int,
    runner: AdbCommandRunner,
    on_seconds: float = 5.0,
) -> FarmTaskResult:
    """Reserved for a future companion command; not wired until a safe command exists."""
    _ = (slot_map, farm_slot_id, runner, on_seconds)
    return FarmTaskResult(
        ok=False,
        http_status=501,
        error="action_not_supported",
        message=AIRPLANE_UNSUPPORTED_REASON,
    )


def verify_slot_health_after_action(
    slot_map: dict[int, str],
    farm_slot_id: int,
    runner: AdbCommandRunner,
) -> dict[str, Any]:
    serial = _serial_for_slot(slot_map, farm_slot_id)
    if not serial:
        return {"healthy": False, "error": "slot_not_found"}
    controller = AdbDeviceHealthController(runner)
    health = controller.read_health(farm_slot_id, serial)
    return {
        "healthy": health.healthy,
        "adb_online": health.adb_online,
        "radio_registered": health.radio_registered,
        "network_reachable": health.network_reachable,
        "error": health.error,
    }
