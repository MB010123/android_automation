from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.provisioning_state import (
    ActivationVerdict,
    InvalidJobTransition,
    JobState,
    apply_transition,
    can_transition,
    is_terminal,
)


def test_partial_is_never_success():
    assert ActivationVerdict.ACTIVATION_PARTIAL.success is False
    assert ActivationVerdict.ACTIVATION_FAILED.success is False
    assert ActivationVerdict.VERIFICATION_UNKNOWN.success is False
    assert ActivationVerdict.ACTIVATION_CONFIRMED.success is True


@pytest.mark.parametrize(
    "current,destination",
    [
        (JobState.PENDING, JobState.CLAIMED),
        (JobState.CLAIMED, JobState.PREFLIGHT),
        (JobState.PREFLIGHT, JobState.WAITING_FOR_ACTIVATION),
        (JobState.PREFLIGHT, JobState.ACTIVATION_SUBMITTED),
        (JobState.WAITING_FOR_ACTIVATION, JobState.ACTIVATION_SUBMITTED),
        (JobState.ACTIVATION_SUBMITTED, JobState.VERIFICATION),
        (JobState.VERIFICATION, JobState.ACTIVE),
        (JobState.VERIFICATION, JobState.WAITING_FOR_ACTIVATION),
        (JobState.VERIFICATION, JobState.RETRYABLE),
        (JobState.VERIFICATION, JobState.FAILED),
        (JobState.RETRYABLE, JobState.CLAIMED),
        (JobState.RETRYABLE, JobState.PREFLIGHT),
        (JobState.CLAIMED, JobState.FAILED),
        (JobState.PREFLIGHT, JobState.FAILED),
    ],
)
def test_allowed_transitions(current, destination):
    assert apply_transition(current, destination) is destination


@pytest.mark.parametrize(
    "current,destination",
    [
        (JobState.PENDING, JobState.ACTIVE),
        (JobState.PENDING, JobState.VERIFICATION),
        (JobState.WAITING_FOR_ACTIVATION, JobState.ACTIVE),
        (JobState.CLAIMED, JobState.WAITING_FOR_ACTIVATION),
        (JobState.ACTIVE, JobState.CLAIMED),
        (JobState.FAILED, JobState.RETRYABLE),
        (JobState.FAILED, JobState.PENDING),
        (JobState.ACTIVE, JobState.FAILED),
        (JobState.VERIFICATION, JobState.CLAIMED),
        (JobState.ACTIVATION_SUBMITTED, JobState.ACTIVE),
        (JobState.WAITING_FOR_ACTIVATION, JobState.PREFLIGHT),
    ],
)
def test_invalid_transitions_are_rejected(current, destination):
    assert can_transition(current, destination) is False
    with pytest.raises(InvalidJobTransition):
        apply_transition(current, destination)


def test_terminal_states():
    assert is_terminal(JobState.ACTIVE) is True
    assert is_terminal(JobState.FAILED) is True
    assert is_terminal(JobState.RETRYABLE) is False
    assert is_terminal(JobState.WAITING_FOR_ACTIVATION) is False
