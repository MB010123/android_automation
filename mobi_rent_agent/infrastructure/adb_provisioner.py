"""Secure ADB adapter for a privileged on-device eSIM companion.

The activation code is sent over an ADB-forwarded local socket instead of
appearing in an ``adb shell`` command line. The companion must invoke
``EuiccManager.downloadSubscription`` and return its callback result as one
newline-delimited JSON object.
"""
from __future__ import annotations

import logging

from domain.models import ActivationJob, ProvisioningResult
from domain.ports import SubscriptionProvisioner
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
    ) -> None:
        self._command_runner = command_runner
        self._client = AdbForwardedJsonClient(
            command_runner,
            companion_socket,
            provisioning_timeout_seconds,
            max_response_bytes,
        )

    def provision(self, serial: str, job: ActivationJob) -> ProvisioningResult:
        try:
            self._preflight(serial)
            response = self._client.request(
                serial,
                {
                    "command": "provision_esim",
                    "job_id": job.job_id,
                    "slot_id": job.slot_id,
                    "activation_code": job.activation_code,
                    "switch_after_download": job.switch_after_download,
                },
            )
            return self._parse_response(job, response)
        except (AdbCommandError, OSError, ValueError) as exc:
            return ProvisioningResult(
                success=False,
                job_id=job.job_id,
                slot_id=job.slot_id,
                error=str(exc),
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
    def _parse_response(job: ActivationJob, response: dict) -> ProvisioningResult:
        success = response.get("success")
        if not isinstance(success, bool):
            raise ValueError("provisioning companion response is missing boolean 'success'")

        device_code = response.get("device_code")
        if device_code is not None and not isinstance(device_code, int):
            raise ValueError("provisioning companion 'device_code' must be an integer")

        error = response.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError("provisioning companion 'error' must be a string")

        active_phone_number = response.get("active_phone_number")
        if active_phone_number is not None and not isinstance(active_phone_number, str):
            raise ValueError("provisioning companion 'active_phone_number' must be a string")

        return ProvisioningResult(
            success=success,
            job_id=job.job_id,
            slot_id=job.slot_id,
            device_code=device_code,
            error=error,
            active_phone_number=active_phone_number,
        )
