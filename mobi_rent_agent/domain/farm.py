"""Farm-wide slot health, SMS outbox status, and isolation constants.

These types are framework-free. They do not talk to ADB, VoidFix, or .env.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


EXPECTED_PRODUCTION_SLOT_COUNT = 20
EXPECTED_PRODUCTION_SLOT_IDS = frozenset(range(1, EXPECTED_PRODUCTION_SLOT_COUNT + 1))
# Isolated Pixel 7a prototype. Never assign this serial to a farm bay.
ISOLATED_PROTOTYPE_SERIALS = frozenset({"3C071JEHN14705"})
PROTOTYPE_VOIDFIX_DEVICE_ID = "1385"


class FarmSlotState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    MAPPING_ERROR = "MAPPING_ERROR"
    SIM_ERROR = "SIM_ERROR"
    PROXY_ERROR = "PROXY_ERROR"
    VOIDFIX_ERROR = "VOIDFIX_ERROR"
    SEND_BLOCKED = "SEND_BLOCKED"
    UNKNOWN = "UNKNOWN"


class OutboundMessageStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    DRY_RUN = "dry_run"


class SmsFinalStatus(str, Enum):
    """Terminal or in-progress delivery lifecycle for VoidFix outbound SMS."""

    QUEUED = "queued"
    ACCEPTED = "accepted"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    TIMEOUT = "timeout"


class FarmCheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


@dataclass(frozen=True)
class FarmCheck:
    name: str
    status: FarmCheckStatus
    detail: str
    slot_id: int | None = None


@dataclass(frozen=True)
class FarmSlotReport:
    slot_id: int
    state: FarmSlotState
    verdict: FarmCheckStatus
    serial_redacted: str | None
    adb_state: str | None
    voidfix_device_id: str | None
    checks: tuple[FarmCheck, ...] = ()
    last_error: str | None = None
    attention: str | None = None


@dataclass(frozen=True)
class FarmPreflightReport:
    overall: FarmCheckStatus
    slots: tuple[FarmSlotReport, ...]
    global_checks: tuple[FarmCheck, ...] = ()
    extra_unmapped_redacted: tuple[str, ...] = ()
    prototype_isolated: bool = False

    @property
    def failing_slots(self) -> list[int]:
        return [slot.slot_id for slot in self.slots if slot.verdict is FarmCheckStatus.FAIL]


@dataclass(frozen=True)
class VoidFixDeliveryPollPolicy:
    interval_seconds: float = 5.0
    timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0 or self.timeout_seconds <= 0:
            raise ValueError("delivery poll interval and timeout must be positive")


@dataclass
class OutboundMessageRecord:
    idempotency_key: str
    job_id: str
    slot_id: int
    status: OutboundMessageStatus
    to_number_redacted: str
    provider_message_id: str | None = None
    voidfix_device_id: str | None = None
    voidfix_sim_slot: int | None = None
    destination_redacted: str | None = None
    accepted_at: float | None = None
    provider_sent_date: str | None = None
    provider_delivered_date: str | None = None
    provider_error_code: str | None = None
    final_status: SmsFinalStatus | None = None
    last_polled_at: float | None = None
    error: str | None = None
    blocked_reason: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


TERMINAL_SMS_FINAL_STATUSES = frozenset(
    {SmsFinalStatus.DELIVERED, SmsFinalStatus.FAILED, SmsFinalStatus.TIMEOUT}
)


def is_terminal_sms_final_status(status: SmsFinalStatus | None) -> bool:
    return status in TERMINAL_SMS_FINAL_STATUSES


def is_safe_retryable_sms(outcome: str | None, error: str | None) -> bool:
    """Retry only clear pre-submit transport failures. Never retry UNKNOWN."""
    if outcome == OutboundMessageStatus.UNKNOWN.value or outcome == "unknown":
        return False
    if not error:
        return False
    return error.startswith("request_error:") and "timeout" not in error.lower()


@dataclass(frozen=True)
class SmsRetryPolicy:
    max_attempts: int = 1
    base_delay_seconds: float = 0.5
    multiplier: float = 2.0
    max_delay_seconds: float = 4.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("sms max_attempts must be 1-3")
        if self.base_delay_seconds <= 0 or self.max_delay_seconds <= 0:
            raise ValueError("sms retry delays must be positive")
        if self.multiplier < 1:
            raise ValueError("sms retry multiplier must be >= 1")

    def delay_for_attempt(self, attempt: int) -> float:
        if attempt <= 0:
            return 0.0
        delay = self.base_delay_seconds * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_delay_seconds)
