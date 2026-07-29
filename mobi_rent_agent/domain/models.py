"""Domain models for the hardware heartbeat agent.

These are plain, framework-free objects. They know nothing about HTTP,
ADB, or config files - that belongs to the infrastructure layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SlotStatus(str, Enum):
    """Allowed values for the `status` field of the heartbeat payload.

    Must match the values expected by the backend payload schema exactly:
    'online', 'busy', 'networkerror'.
    """

    ONLINE = "online"
    BUSY = "busy"
    NETWORK_ERROR = "networkerror"


@dataclass(frozen=True)
class SlotState:
    """Represents the current observed state of a single physical slot.

    Attributes:
        slot_id: Target slot number, 1-20, fixed per physical position
            (not dependent on USB/ADB connection order).
        status: Current lifecycle status of the slot.
    """

    slot_id: int
    status: SlotStatus

    def __post_init__(self) -> None:
        if not (1 <= self.slot_id <= 20):
            raise ValueError(f"slot_id must be between 1 and 20, got {self.slot_id}")


@dataclass(frozen=True)
class HeartbeatPayload:
    """Wire-format-agnostic representation of a heartbeat request body.

    Field names are intentionally identical to the backend schema so the
    infrastructure-layer serializer stays a trivial 1:1 mapping:
        hardware_agent_token, slot_id, status
    """

    hardware_agent_token: str
    slot_id: int
    status: SlotStatus

    def to_dict(self) -> dict:
        return {
            "hardware_agent_token": self.hardware_agent_token,
            "slot_id": self.slot_id,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class HeartbeatResult:
    """Outcome of attempting to send a single heartbeat."""

    success: bool
    slot_id: int
    status_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ActivationJob:
    """A backend-issued eSIM activation assigned to one physical slot."""

    job_id: str
    slot_id: int
    activation_code: str | None = None
    qr_url: str | None = None
    switch_after_download: bool = True

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id must not be empty")
        if not (1 <= self.slot_id <= 20):
            raise ValueError(f"slot_id must be between 1 and 20, got {self.slot_id}")
        activation_code = self.activation_code.strip() if self.activation_code else ""
        qr_url = self.qr_url.strip() if self.qr_url else ""
        if bool(activation_code) == bool(qr_url):
            raise ValueError("provide exactly one of activation_code or qr_url")

    def with_activation_code(self, activation_code: str) -> ActivationJob:
        return ActivationJob(
            job_id=self.job_id,
            slot_id=self.slot_id,
            activation_code=activation_code,
            switch_after_download=self.switch_after_download,
        )


@dataclass(frozen=True)
class ProvisioningResult:
    """Final result reported by the device provisioning companion."""

    success: bool
    job_id: str
    slot_id: int
    device_code: int | None = None
    error: str | None = None
    active_phone_number: str | None = None

    def to_dict(self) -> dict:
        payload: dict[str, str | int | bool] = {
            "job_id": self.job_id,
            "slot_id": self.slot_id,
            "success": self.success,
        }
        if self.device_code is not None:
            payload["device_code"] = self.device_code
        if self.error:
            payload["error"] = self.error
        if self.active_phone_number:
            payload["active_phone_number"] = self.active_phone_number
        return payload


@dataclass(frozen=True)
class ProxyRoute:
    """A persistent SOCKS5 assignment for one physical slot."""

    slot_id: int
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        if not (1 <= self.slot_id <= 20):
            raise ValueError(f"slot_id must be between 1 and 20, got {self.slot_id}")
        if not self.host.strip():
            raise ValueError("proxy host must not be empty")
        if not (1 <= self.port <= 65535):
            raise ValueError(f"proxy port must be between 1 and 65535, got {self.port}")
        if (self.username is None) != (self.password is None):
            raise ValueError("proxy username and password must be provided together")

    @property
    def route_key(self) -> tuple[str, int, str | None]:
        return (self.host.lower(), self.port, self.username)


class RecoveryAction(str, Enum):
    NONE = "none"
    REBOOT = "reboot"


@dataclass(frozen=True)
class DeviceHealth:
    """One radio/network health observation for a slot."""

    slot_id: int
    adb_online: bool
    boot_completed: bool
    radio_registered: bool
    network_reachable: bool
    signal_summary: str | None = None
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return (
            self.adb_online
            and self.boot_completed
            and self.radio_registered
            and self.network_reachable
        )
