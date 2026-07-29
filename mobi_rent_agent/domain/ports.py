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
    ProvisioningResult,
    ProxyRoute,
    SlotState,
)


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
