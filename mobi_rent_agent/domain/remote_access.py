"""Remote-access proof-of-concept domain types.

The remote-access platform (GADS) only provides screen streaming, remote
input and a temporary device lease. Rental ownership, slot assignment,
QR security, eSIM records and customer authentication stay in the
Mobi-Rent backend. Nothing here touches EuiccManager or eSIM permissions:
the customer performs the normal Android eSIM UI flow remotely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class RemoteAccessError(RuntimeError):
    """Base class for remote-access failures."""


class RemoteAccessForbidden(RemoteAccessError):
    """The authorization chain customer -> rental -> slot -> device failed."""


class RemoteAccessUnavailable(RemoteAccessError):
    """The POC is disabled or the platform is not configured/reachable."""


class RemoteAccessPlatformError(RemoteAccessError):
    """The platform rejected or failed a request."""


@dataclass(frozen=True)
class RemoteDeviceStatus:
    slot_id: int
    device_id: str
    registered: bool
    online: bool
    available: bool
    in_use_by: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_public_dict(self) -> dict[str, Any]:
        """Browser-safe view: never includes platform credentials or raw payload."""
        if not self.registered:
            state = "unregistered"
        elif not self.online:
            state = "offline"
        elif not self.available:
            state = "busy"
        else:
            state = "online"
        return {
            "slot_id": self.slot_id,
            "state": state,
            "online": self.online,
            "available": self.available,
            "busy": self.online and not self.available,
        }


@dataclass(frozen=True)
class PlatformAccessGrant:
    """Result of creating a platform-side lease for one rental."""

    device_id: str
    platform_username: str
    platform_password: str
    access_url: str
    expires_at: float


class RemoteAccessPlatform(Protocol):
    """Adapter boundary. Implementations must never be reachable from the browser."""

    def grant_access(
        self,
        *,
        device_id: str,
        rental_id: str,
        ttl_minutes: int,
        workspace_id: str,
    ) -> PlatformAccessGrant: ...

    def revoke_access(self, *, device_id: str, platform_username: str) -> bool: ...

    def release_device(self, *, device_id: str) -> bool: ...

    def device_status(
        self,
        *,
        slot_id: int,
        device_id: str,
        workspace_id: str | None = None,
    ) -> RemoteDeviceStatus: ...


FORBIDDEN_REASONS = frozenset(
    {
        "poc_disabled",
        "slot_not_allowlisted",
        "unauthenticated",
        "rental_not_found",
        "rental_not_owned",
        "rental_slot_mismatch",
        "slot_not_mapped",
        "access_not_active",
        "access_expired",
        "rental_expired",
    }
)

# Customer-facing activation machine. Internal prepare_state stays more granular.
ACTIVATION_REMOTE_ACCESS_READY = "REMOTE_ACCESS_READY"
ACTIVATION_QR_READY = "QR_READY"
ACTIVATION_DEVICE_REBOOTING = "DEVICE_REBOOTING"
ACTIVATION_CUSTOMER_REQUIRED = "CUSTOMER_ACTIVATION_REQUIRED"
ACTIVATION_ACTIVATING = "ACTIVATING"
ACTIVATION_ACTIVE = "ACTIVE"
ACTIVATION_FAILED = "FAILED"

ACTIVATION_GUIDANCE = {
    ACTIVATION_REMOTE_ACCESS_READY: (
        "Open the Pixel remotely. When you are ready, continue so we can place the eSIM QR on the phone."
    ),
    ACTIVATION_DEVICE_REBOOTING: (
        "The Pixel is preparing and may reboot. Wait until the remote screen is available again."
    ),
    ACTIVATION_QR_READY: (
        "Your eSIM QR is ready on the phone. Open the Pixel remotely and follow the Android SIM setup screen."
    ),
    ACTIVATION_CUSTOMER_REQUIRED: (
        "Your eSIM QR is ready on the phone. Open the Pixel remotely and follow the Android SIM setup screen."
    ),
    ACTIVATION_ACTIVATING: "Checking whether the eSIM finished activating. This is not a success yet.",
    ACTIVATION_ACTIVE: "eSIM activation is confirmed on the device.",
    ACTIVATION_FAILED: "Preparing the eSIM QR failed. Retry prepare-esim or contact support.",
}

_PREPARE_REBOOTING_STATES = frozenset({"placing_qr", "rebooting", "waiting_adb", "waiting_platform"})


def public_activation_view(
    *,
    prepare_state: str | None,
    activation_observed: str | None,
) -> dict[str, Any]:
    """Browser-safe activation fields. Never includes serials, QR URLs, or codes.

    Four-layer observation only promotes to ACTIVE on ACTIVATION_CONFIRMED.
    Missing/absent eSIM after QR placement stays CUSTOMER_ACTIVATION_REQUIRED
    (not FAILED): the customer has not confirmed in Android Settings yet.
    """
    observed = str(activation_observed or "").strip().lower()
    prepare = str(prepare_state or "").strip()
    if prepare == "failed":
        state = ACTIVATION_FAILED
    elif observed == "confirmed":
        state = ACTIVATION_ACTIVE
    elif observed == "partial":
        state = ACTIVATION_ACTIVATING
    elif prepare in _PREPARE_REBOOTING_STATES:
        state = ACTIVATION_DEVICE_REBOOTING
    elif prepare == "ready":
        state = ACTIVATION_CUSTOMER_REQUIRED
    else:
        state = ACTIVATION_REMOTE_ACCESS_READY
    qr_ready = prepare == "ready" or state in {ACTIVATION_CUSTOMER_REQUIRED, ACTIVATION_ACTIVATING, ACTIVATION_ACTIVE}
    return {
        "activation_state": state,
        "qr_ready": qr_ready,
        "guidance": ACTIVATION_GUIDANCE.get(state, ACTIVATION_GUIDANCE[ACTIVATION_CUSTOMER_REQUIRED]),
    }
