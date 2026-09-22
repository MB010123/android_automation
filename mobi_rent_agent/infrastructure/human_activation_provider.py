"""Human Settings/LPA activation provider.

Never sends an activation code, never calls EuiccManager, and never
mutates SIM/eSIM state. Used as the only wired Phase 2 provider until a
privileged/carrier/managed-device implementation is separately authorized.
"""
from __future__ import annotations

from domain.models import ActivationJob, ProvisioningResult
from domain.ports import EsimProvisioningProvider, ProviderCapabilities, SubmitResult, SubscriptionProvisioner
from domain.provisioning_state import ActivationVerdict, JobState
from domain.verification import FourLayerVerification, evaluate_activation


class HumanActivationProvider(EsimProvisioningProvider, SubscriptionProvisioner):
    PROVIDER_ID = "human"

    def capabilities(self, serial: str | None = None) -> ProviderCapabilities:
        return ProviderCapabilities(
            unattended=False,
            can_download=False,
            can_switch=False,
            can_delete=False,
            requires_user_consent=True,
            provider_id=self.PROVIDER_ID,
            authorization_source="none",
            reason="human Settings/LPA; no unattended Android eSIM authority",
        )

    def submit_activation(self, serial: str, job: ActivationJob) -> SubmitResult:
        _ = serial
        _ = job.activation_code
        return SubmitResult(
            accepted=False,
            state=JobState.WAITING_FOR_ACTIVATION,
            error="human Settings/LPA required; activation code was not sent",
            activation_code_sent=False,
        )

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
        return SubmitResult(
            accepted=False,
            state=JobState.FAILED,
            error="activate_profile is refused by HumanActivationProvider",
            activation_code_sent=False,
        )

    def provision(self, serial: str, job: ActivationJob) -> ProvisioningResult:
        """SubscriptionProvisioner seam: wait for human activation. Never submits LPA."""
        submitted = self.submit_activation(serial, job)
        return ProvisioningResult(
            success=False,
            job_id=job.job_id,
            slot_id=job.slot_id,
            error=submitted.error,
        )
