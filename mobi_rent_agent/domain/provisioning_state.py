"""Provisioning job states, verdicts, and explicit transitions."""
from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PREFLIGHT = "preflight"
    WAITING_FOR_ACTIVATION = "waiting_for_activation"
    ACTIVATION_SUBMITTED = "activation_submitted"
    VERIFICATION = "verification"
    ACTIVE = "active"
    RETRYABLE = "retryable"
    FAILED = "failed"


class ActivationVerdict(str, Enum):
    ACTIVATION_CONFIRMED = "ACTIVATION_CONFIRMED"
    ACTIVATION_PARTIAL = "ACTIVATION_PARTIAL"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"
    VERIFICATION_UNKNOWN = "VERIFICATION_UNKNOWN"

    @property
    def success(self) -> bool:
        """Only CONFIRMED is success. PARTIAL is never success."""
        return self is ActivationVerdict.ACTIVATION_CONFIRMED


class InvalidJobTransition(ValueError):
    """Raised when a job state transition is not in the approved table."""


# Non-terminal → failed is always allowed (isolation / safety abort).
_ALLOWED: frozenset[tuple[JobState, JobState]] = frozenset(
    {
        (JobState.PENDING, JobState.CLAIMED),
        (JobState.CLAIMED, JobState.PREFLIGHT),
        (JobState.CLAIMED, JobState.FAILED),
        (JobState.CLAIMED, JobState.RETRYABLE),
        (JobState.PREFLIGHT, JobState.WAITING_FOR_ACTIVATION),
        (JobState.PREFLIGHT, JobState.ACTIVATION_SUBMITTED),
        (JobState.PREFLIGHT, JobState.FAILED),
        (JobState.PREFLIGHT, JobState.RETRYABLE),
        (JobState.WAITING_FOR_ACTIVATION, JobState.ACTIVATION_SUBMITTED),
        (JobState.WAITING_FOR_ACTIVATION, JobState.FAILED),
        (JobState.ACTIVATION_SUBMITTED, JobState.VERIFICATION),
        (JobState.ACTIVATION_SUBMITTED, JobState.FAILED),
        (JobState.VERIFICATION, JobState.ACTIVE),
        (JobState.VERIFICATION, JobState.WAITING_FOR_ACTIVATION),
        (JobState.VERIFICATION, JobState.RETRYABLE),
        (JobState.VERIFICATION, JobState.FAILED),
        (JobState.RETRYABLE, JobState.CLAIMED),
        (JobState.RETRYABLE, JobState.PREFLIGHT),
        (JobState.RETRYABLE, JobState.FAILED),
    }
)

_TERMINAL = frozenset({JobState.ACTIVE, JobState.FAILED})


def can_transition(current: JobState, destination: JobState) -> bool:
    if current is destination:
        return False
    return (current, destination) in _ALLOWED


def apply_transition(current: JobState, destination: JobState) -> JobState:
    if not can_transition(current, destination):
        raise InvalidJobTransition(
            f"invalid provisioning transition {current.value} → {destination.value}"
        )
    return destination


def is_terminal(state: JobState) -> bool:
    return state in _TERMINAL
