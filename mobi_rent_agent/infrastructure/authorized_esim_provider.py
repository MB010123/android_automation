"""Production eSIM provider behind EsimProvisioningProvider.

REAL_ESIM_ENABLED is not authorization. Live download is armed only when
the allowlist is exactly {1}, ESIM_LIVE_DOWNLOAD_ARMED is true, a live
Android capability gate passes, and a download transport is attached.
Activation codes are never logged or written to disk.
"""
from __future__ import annotations

from collections.abc import Callable

from domain.esim_capabilities import (
    LEGITIMATE_AUTHORIZATION_SOURCES,
    AndroidAuthorizationSnapshot,
    derive_esim_capabilities,
)
from domain.models import ActivationJob, ProvisioningResult
from domain.ports import EsimProvisioningProvider, ProviderCapabilities, SubmitResult, SubscriptionProvisioner
from domain.provisioning_state import ActivationVerdict, JobState
from domain.slot_isolation import SlotIsolationPolicy
from domain.verification import FourLayerVerification, evaluate_activation
from infrastructure.android_authorization_probe import AuthorizationProbe, StaticAuthorizationProbe
from infrastructure.human_activation_provider import HumanActivationProvider

UNKNOWN_SOURCES = frozenset({"", "none", "unknown"})
DownloadTransport = Callable[[str, ActivationJob], dict]


def companion_provision_transport(client) -> DownloadTransport:
    """Send provision_esim without logging the activation payload."""

    def _send(serial: str, job: ActivationJob) -> dict:
        payload: dict = {
            "command": "provision_esim",
            "job_id": job.job_id,
            "slot_id": job.slot_id,
            "switch_after_download": bool(job.switch_after_download),
        }
        if job.activation_code:
            payload["activation_code"] = job.activation_code
        response = client.request(serial, payload)
        if not isinstance(response, dict):
            return {"success": False, "error": "invalid companion response"}
        response.pop("activation_code", None)
        response.pop("qr_url", None)
        return response

    return _send


def live_download_may_arm(
    *,
    real_esim_enabled: bool,
    esim_live_download_armed: bool,
    allowed_slot_ids: frozenset[int],
) -> bool:
    """REAL_ESIM_ENABLED alone cannot arm download. Slot 1 allowlist required."""
    return (
        real_esim_enabled is True
        and esim_live_download_armed is True
        and allowed_slot_ids == frozenset({1})
    )


def download_may_switch_after(caps, requested: bool) -> bool:
    """Honor enable-after-download on the public download API.

    Device Owner may request enable-after-download. That is not a later
    profile-switch grant (can_switch) and never enables delete.
    """
    if not requested:
        return False
    if caps.can_switch:
        return True
    return caps.authorization_source in {"device_owner", "profile_owner"}


class AuthorizedEsimProvider(EsimProvisioningProvider, SubscriptionProvisioner):
    PROVIDER_ID = "authorized"

    def __init__(
        self,
        probe: AuthorizationProbe,
        isolation: SlotIsolationPolicy | None = None,
        *,
        live_download_armed: bool = False,
        required_sources: frozenset[str] | None = None,
        download_transport: DownloadTransport | None = None,
        verification_source: Callable[[str, ActivationJob], FourLayerVerification | None] | None = None,
    ) -> None:
        self._probe = probe
        self._isolation = isolation or SlotIsolationPolicy()
        self._live_download_armed = live_download_armed
        self._required_sources = required_sources
        self._download_transport = download_transport
        self._verification_source = verification_source

    def esim_capabilities(self, serial: str | None = None):
        return derive_esim_capabilities(self._probe.read(serial))

    def capabilities(self, serial: str | None = None) -> ProviderCapabilities:
        return self.esim_capabilities(serial).to_provider_capabilities()

    def submit_activation(self, serial: str, job: ActivationJob) -> SubmitResult:
        if not self._isolation.allows(job.slot_id):
            return self._refuse(
                JobState.FAILED,
                f"slot {job.slot_id} is outside the provisioning allowlist "
                f"{sorted(self._isolation.allowed_slot_ids)}",
            )
        caps = self.esim_capabilities(serial)
        if caps.authorization_source in UNKNOWN_SOURCES:
            return self._refuse(
                JobState.WAITING_FOR_ACTIVATION,
                f"no legitimate Android eSIM authority ({caps.reason})",
            )
        if self._required_sources is not None and caps.authorization_source not in self._required_sources:
            return self._refuse(
                JobState.WAITING_FOR_ACTIVATION,
                "managed eSIM path requires Device Owner or Profile Owner",
            )
        if not caps.unattended or not caps.can_download:
            return self._refuse(
                JobState.WAITING_FOR_ACTIVATION,
                "can_download=false; public eUICC download is not permitted",
            )
        if caps.authorization_source not in LEGITIMATE_AUTHORIZATION_SOURCES:
            return self._refuse(
                JobState.WAITING_FOR_ACTIVATION,
                "authorization_source is not a legitimate Android authority",
            )
        if not self._live_download_armed:
            return self._refuse(
                JobState.WAITING_FOR_ACTIVATION,
                "capability gate passed but live download is not armed; "
                "separate Slot 1 activation authorization is required",
            )
        if self._download_transport is None:
            return self._refuse(
                JobState.WAITING_FOR_ACTIVATION,
                "live download is armed but no download transport is attached",
            )
        if not job.activation_code:
            return self._refuse(
                JobState.WAITING_FOR_ACTIVATION,
                "activation payload missing; live download not sent",
            )
        switch_after = download_may_switch_after(caps, bool(job.switch_after_download))
        job_to_send = job if switch_after == job.switch_after_download else ActivationJob(
            job.job_id,
            job.slot_id,
            job.activation_code,
            switch_after_download=False,
        )
        response = self._download_transport(serial, job_to_send)
        if response.get("success") is True and response.get("device_code") in (0, None):
            return SubmitResult(
                accepted=True,
                state=JobState.ACTIVATION_SUBMITTED,
                activation_code_sent=True,
            )
        error = str(response.get("error") or "device download refused")
        return self._refuse(JobState.WAITING_FOR_ACTIVATION, error)

    def wait_for_result(self, serial: str, job: ActivationJob, timeout_seconds: float) -> SubmitResult:
        _ = timeout_seconds
        return self.submit_activation(serial, job)

    def verify_profile(
        self,
        serial: str,
        job: ActivationJob,
        snapshot: FourLayerVerification,
    ) -> ProvisioningResult:
        _ = serial
        verdict = evaluate_activation(snapshot)
        error = None
        if verdict is ActivationVerdict.ACTIVATION_PARTIAL:
            error = "Settings/LPA evidence present; SubscriptionManager/RIL/connectivity incomplete"
        elif verdict is ActivationVerdict.VERIFICATION_UNKNOWN:
            error = "verification observation is insufficient"
        elif verdict is ActivationVerdict.ACTIVATION_FAILED:
            error = snapshot.failure_reason or "activation failed"
        return ProvisioningResult.from_verdict(
            job_id=job.job_id,
            slot_id=job.slot_id,
            verdict=verdict,
            error=error,
        )

    def activate_profile(self, serial: str, job: ActivationJob, subscription_id: int) -> SubmitResult:
        _ = serial
        _ = subscription_id
        caps = self.esim_capabilities(serial)
        if caps.can_delete:
            return self._refuse(JobState.FAILED, "can_delete must remain false")
        if not job.switch_after_download:
            return self._refuse(
                JobState.FAILED,
                "switch_after_download defaults false and is not authorized",
            )
        if not caps.can_switch or caps.authorization_source in UNKNOWN_SOURCES:
            return self._refuse(
                JobState.FAILED,
                "activate_profile refused: no legitimate switch permission",
            )
        return self._refuse(
            JobState.FAILED,
            "profile switch is not armed; no automatic switch",
        )

    def provision(self, serial: str, job: ActivationJob) -> ProvisioningResult:
        submitted = self.submit_activation(serial, job)
        if not submitted.accepted:
            return ProvisioningResult(
                success=False,
                job_id=job.job_id,
                slot_id=job.slot_id,
                error=submitted.error,
            )
        if self._verification_source is None:
            return ProvisioningResult.from_verdict(
                job_id=job.job_id,
                slot_id=job.slot_id,
                verdict=ActivationVerdict.VERIFICATION_UNKNOWN,
                error="download accepted; four-layer verification snapshot missing",
            )
        snapshot = self._verification_source(serial, job)
        if snapshot is None:
            return ProvisioningResult.from_verdict(
                job_id=job.job_id,
                slot_id=job.slot_id,
                verdict=ActivationVerdict.VERIFICATION_UNKNOWN,
                error="four-layer verification observation is incomplete",
            )
        return self.verify_profile(serial, job, snapshot)

    @staticmethod
    def _refuse(state: JobState, error: str) -> SubmitResult:
        return SubmitResult(
            accepted=False,
            state=state,
            error=error,
            activation_code_sent=False,
        )


def select_esim_provider(
    snapshot: AndroidAuthorizationSnapshot,
    isolation: SlotIsolationPolicy | None = None,
    *,
    live_download_armed: bool = False,
    download_transport: DownloadTransport | None = None,
    verification_source: Callable[[str, ActivationJob], FourLayerVerification | None] | None = None,
) -> EsimProvisioningProvider:
    """Choose a provider. REAL_ESIM_ENABLED on the snapshot cannot grant auth."""
    policy = isolation or SlotIsolationPolicy()
    caps = derive_esim_capabilities(snapshot)
    if (
        caps.unattended
        and caps.can_download
        and caps.authorization_source in LEGITIMATE_AUTHORIZATION_SOURCES
    ):
        return AuthorizedEsimProvider(
            StaticAuthorizationProbe(snapshot),
            policy,
            live_download_armed=live_download_armed,
            download_transport=download_transport,
            verification_source=verification_source,
        )
    return HumanActivationProvider()
