"""Quarantined ADB adapter for a future privileged companion.

This class is not wired into the daemon. It refuses unless every safety
gate passes, and even then it never reports success=true (that requires
four-layer ACTIVATION_CONFIRMED). Activation codes are never logged.
"""
from __future__ import annotations

import logging

from domain.models import ActivationJob, ProvisioningResult
from domain.ports import SubscriptionProvisioner
from domain.provisioning_state import ActivationVerdict
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.legacy_silent_guard import refuse_legacy_silent_provision
from infrastructure.adb_companion import (
    AdbCommandError,
    AdbCommandRunner,
    AdbForwardedJsonClient,
)

logger = logging.getLogger("mobi_rent_agent.adb_provisioner")


class AdbCompanionProvisioner(SubscriptionProvisioner):
    def __init__(
        self,
        command_runner: AdbCommandRunner,
        companion_socket: str = "mobi_rent.provisioning",
        provisioning_timeout_seconds: float = 180.0,
        max_response_bytes: int = 64 * 1024,
        isolation: SlotIsolationPolicy | None = None,
        silent_provision_authorized: bool = False,
    ) -> None:
        self._command_runner = command_runner
        self._isolation = isolation or SlotIsolationPolicy()
        self._silent_provision_authorized = silent_provision_authorized
        self._client = AdbForwardedJsonClient(
            command_runner,
            companion_socket,
            provisioning_timeout_seconds,
            max_response_bytes,
        )

    def provision(self, serial: str, job: ActivationJob) -> ProvisioningResult:
        try:
            if not self._isolation.allows(job.slot_id):
                return self._refused(
                    job,
                    f"slot {job.slot_id} is outside the provisioning allowlist "
                    f"{sorted(self._isolation.allowed_slot_ids)}",
                )
            self._preflight(serial)
            status = self._client.request(serial, {"command": "get_esim_status"})
            reason = refuse_legacy_silent_provision(
                slot_id=job.slot_id,
                isolation=self._isolation,
                real_esim_enabled=status.get("real_esim_enabled") is True,
                can_silent_install=status.get("can_silent_install") is True,
                authorized=self._silent_provision_authorized,
            )
            if reason is None:
                reason = (
                    "legacy silent provisioner is quarantined; "
                    "HumanActivationProvider is required"
                )
            return self._refused(job, reason)
        except (AdbCommandError, OSError, ValueError) as exc:
            return ProvisioningResult(
                success=False,
                job_id=job.job_id,
                slot_id=job.slot_id,
                error=str(exc),
                verdict=ActivationVerdict.ACTIVATION_FAILED,
            )

    def _preflight(self, serial: str) -> None:
        state = self._command_runner.run(serial, ["get-state"]).stdout
        if state != "device":
            raise AdbCommandError(f"device {serial} is not online (state={state or 'unknown'})")
        boot_completed = self._command_runner.run(
            serial,
            ["shell", "getprop", "sys.boot_completed"],
        ).stdout
        if boot_completed != "1":
            raise AdbCommandError(f"device {serial} has not completed boot")
        device_features = self._command_runner.run(
            serial,
            ["shell", "pm", "list", "features"],
        ).stdout
        if "feature:android.hardware.telephony.euicc" not in device_features.splitlines():
            raise AdbCommandError(f"device {serial} does not expose the eUICC feature")

    @staticmethod
    def _refused(job: ActivationJob, error: str) -> ProvisioningResult:
        logger.info("Refusing legacy silent provision for slot %s job=%s", job.slot_id, job.job_id)
        return ProvisioningResult.from_verdict(
            job_id=job.job_id,
            slot_id=job.slot_id,
            verdict=ActivationVerdict.ACTIVATION_FAILED,
            error=error,
            device_code=-1,
        )
