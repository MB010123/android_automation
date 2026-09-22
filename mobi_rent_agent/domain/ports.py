"""Ports (abstract interfaces) that the application layer depends on.

Following the Dependency Inversion Principle, the application layer only
knows about these abstractions. Concrete implementations live in
`infrastructure/` and are wired together in `main.py` (the composition
root).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from domain.models import (
    ActivationJob,
    DeviceHealth,
    HeartbeatPayload,
    HeartbeatResult,
    InboundSms,
    ProvisioningResult,
    ProxyRoute,
    SlotState,
    SmsSendResult,
)
from domain.provisioning_state import JobState
from domain.verification import FourLayerVerification


class HeartbeatTransport(ABC):
    """Sends a heartbeat payload to the remote backend."""

    @abstractmethod
    def send(self, payload: HeartbeatPayload) -> HeartbeatResult:
        """Send one heartbeat. Must not raise for network-level failures;
        those are reported through `HeartbeatResult`."""
        raise NotImplementedError


class SlotStatusProvider(ABC):
    """Reads the current status of the physical slots managed by this box."""

    @abstractmethod
    def read_slot_states(self) -> Iterable[SlotState]:
        """Return the current state of every slot known to this agent."""
        raise NotImplementedError


class Clock(ABC):
    """Abstraction over time so the heartbeat loop is testable without
    real sleeping."""

    @abstractmethod
    def sleep(self, seconds: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def monotonic(self) -> float:
        raise NotImplementedError


class ActivationJobSource(ABC):
    """Claims activation jobs and reports their terminal outcomes."""

    @abstractmethod
    def fetch_pending(self, slot_ids: Iterable[int]) -> Iterable[ActivationJob]:
        """Return jobs assigned to this agent's slots."""
        raise NotImplementedError

    @abstractmethod
    def report_result(self, result: ProvisioningResult) -> None:
        """Report a terminal device result. Raise if acknowledgement fails."""
        raise NotImplementedError


class SubscriptionProvisioner(ABC):
    """Installs an activation payload on one ADB-addressed device."""

    @abstractmethod
    def provision(self, serial: str, job: ActivationJob) -> ProvisioningResult:
        """Provision one job and return the device callback result."""
        raise NotImplementedError


class ProviderCapabilities:
    """Static capability bits for an EsimProvisioningProvider."""

    def __init__(
        self,
        *,
        unattended: bool,
        can_download: bool,
        can_switch: bool,
        can_delete: bool,
        requires_user_consent: bool,
        provider_id: str,
        authorization_source: str = "none",
        reason: str = "",
    ) -> None:
        if can_delete:
            raise ValueError("can_delete must remain false")
        self.unattended = unattended
        self.can_download = can_download
        self.can_switch = can_switch
        self.can_delete = False
        self.requires_user_consent = requires_user_consent
        self.provider_id = provider_id
        self.authorization_source = authorization_source
        self.reason = reason


class SubmitResult:
    """Outcome of submit_activation. Never carries an activation code."""

    def __init__(
        self,
        *,
        accepted: bool,
        state: JobState,
        error: str | None = None,
        activation_code_sent: bool = False,
    ) -> None:
        self.accepted = accepted
        self.state = state
        self.error = error
        self.activation_code_sent = activation_code_sent


class EsimProvisioningProvider(ABC):
    """Activation backend. HumanActivationProvider is the fallback.

    AuthorizedEsimProvider may be selected only after a live capability
    gate proves a legitimate Android authorization_source. REAL_ESIM_ENABLED
    is never sufficient by itself.
    """

    @abstractmethod
    def capabilities(self, serial: str | None = None) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    def submit_activation(self, serial: str, job: ActivationJob) -> SubmitResult:
        """Must not send an LPA code unless capabilities.can_download is true."""
        raise NotImplementedError

    @abstractmethod
    def wait_for_result(self, serial: str, job: ActivationJob, timeout_seconds: float) -> SubmitResult:
        raise NotImplementedError

    @abstractmethod
    def verify_profile(
        self,
        serial: str,
        job: ActivationJob,
        snapshot: FourLayerVerification,
    ) -> ProvisioningResult:
        raise NotImplementedError

    @abstractmethod
    def activate_profile(self, serial: str, job: ActivationJob, subscription_id: int) -> SubmitResult:
        raise NotImplementedError


class ActivationPayloadResolver(ABC):
    """Resolves a backend activation string or QR image into an LPA code."""

    @abstractmethod
    def resolve(self, job: ActivationJob) -> ActivationJob:
        """Return the same job populated with an activation code."""
        raise NotImplementedError


class ProxyConfigurator(ABC):
    """Applies and verifies a full-device SOCKS5 route."""

    @abstractmethod
    def ensure_route(self, serial: str, route: ProxyRoute) -> None:
        """Apply the route idempotently, raising if verification fails."""
        raise NotImplementedError


class DeviceHealthController(ABC):
    """Observes and recovers one Android device without global side effects."""

    @abstractmethod
    def read_health(self, slot_id: int, serial: str) -> DeviceHealth:
        raise NotImplementedError

    @abstractmethod
    def reboot(self, serial: str) -> None:
        raise NotImplementedError


class SmsGateway(ABC):
    """Sends and receives SMS through an external gateway provider.

    Phase 2 preparation: implementations must stay inert until explicitly
    configured with credentials, and must never log message bodies or keys.
    """

    @abstractmethod
    def send(
        self,
        to_number: str,
        message: str,
        device_ids: Iterable[str],
        *,
        sim_slot: int | None = None,
    ) -> SmsSendResult:
        """Send one SMS. Must not raise for provider-level failures; those
        are reported through `SmsSendResult`."""
        raise NotImplementedError

    @abstractmethod
    def fetch_inbound(self) -> Iterable[InboundSms]:
        """Return inbound messages. Raise `SmsGatewayError` when the
        inbound channel is not configured for this account."""
        raise NotImplementedError

    @abstractmethod
    def ingest_inbound(self, payload: object) -> Iterable[InboundSms]:
        """Parse a VoidFix dashboard webhook / push payload into inbound SMS.

        Raise `SmsGatewayError` when the payload cannot be parsed. Must not
        log message bodies.
        """
        raise NotImplementedError
