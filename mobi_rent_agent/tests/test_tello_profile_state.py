from __future__ import annotations

from infrastructure.tello_profile_state import (
    PROFILE_STATE_DISABLED,
    PROFILE_STATE_ENABLED,
    parse_sandbox_profile,
    parse_tello_profile,
    switch_after_download_reached_enabled,
)

ISUB_DISABLED = """
[SubscriptionInfoInternal: id=1 iccId=REDACTED simSlotIndex=-1 portIndex=-1 isEmbedded=1 displayName=Tello carrierName= mcc=310 mnc=240
Embedded subscriptions: [1]
"""

ISUB_ENABLED = """
[SubscriptionInfoInternal: id=1 iccId=REDACTED simSlotIndex=0 portIndex=0 isEmbedded=1 displayName=Tello carrierName=Tello mcc=310 mnc=240
Embedded subscriptions: [1]
"""

LPA_DISABLED = (
    "EuiccProfileInfo (nickname=Tello, serviceProviderName=Tello, profileName=Tello, "
    "profileClass=2, state=0, CarrierIdentifier=CarrierIdentifier{mcc=310,mnc=240})"
)
LPA_ENABLED = (
    "EuiccProfileInfo (nickname=Tello, serviceProviderName=Tello, profileName=Tello, "
    "profileClass=2, state=1, CarrierIdentifier=CarrierIdentifier{mcc=310,mnc=240})"
)


def test_parse_disabled_tello_from_isub():
    obs = parse_tello_profile(ISUB_DISABLED, "")
    assert obs.present is True
    assert obs.state == PROFILE_STATE_DISABLED
    assert obs.sim_slot_index == -1
    assert obs.subscription_id == 1
    ok, reason = switch_after_download_reached_enabled(obs)
    assert ok is False
    assert reason is not None and "state=0" in reason


def test_parse_enabled_tello_prefers_lpa_state():
    obs = parse_tello_profile(ISUB_DISABLED, LPA_ENABLED)
    assert obs.state == PROFILE_STATE_ENABLED
    assert obs.source == "lpa"


def test_switch_after_download_requires_state_one():
    obs = parse_tello_profile(ISUB_ENABLED, LPA_ENABLED)
    ok, reason = switch_after_download_reached_enabled(obs)
    assert ok is True
    assert reason is None
    assert obs.state == 1


def test_missing_tello_fails_switch_after_validation():
    obs = parse_tello_profile("Embedded subscriptions: []", "")
    ok, reason = switch_after_download_reached_enabled(obs)
    assert ok is False
    assert "not present" in (reason or "")


def test_lpa_enabled_but_unmapped_sim_slot_fails_switch_after():
    obs = parse_tello_profile(ISUB_DISABLED, LPA_ENABLED)
    ok, reason = switch_after_download_reached_enabled(obs)
    assert ok is False
    assert reason is not None and "not mapped" in reason


ISUB_MIXED = """
[SubscriptionInfoInternal: id=2 iccId=REDACTED simSlotIndex=1 portIndex=0 isEmbedded=1 displayName=US Mobile carrierName=US Mobile mcc=311 mnc=480
[SubscriptionInfoInternal: id=1 iccId=REDACTED simSlotIndex=-1 portIndex=-1 isEmbedded=1 displayName=Tello carrierName=Tello mcc=310 mnc=240
Embedded subscriptions: [1, 2]
"""

ISUB_UNKNOWN_CARRIER = """
[SubscriptionInfoInternal: id=9 iccId=REDACTED simSlotIndex=0 portIndex=0 isEmbedded=1 displayName=Acme Wireless carrierName=Acme mcc=001 mnc=01
[SubscriptionInfoInternal: id=1 iccId=REDACTED simSlotIndex=-1 portIndex=-1 isEmbedded=1 displayName=Tello carrierName=Tello mcc=310 mnc=240
Embedded subscriptions: [1, 9]
"""

REGISTRY_HOME = """
NetworkRegistrationInfo{ domain=PS transportType=WWAN registrationState=HOME
mAlphaLong=US Mobile mAlphaShort=US Mobile
mIsEmergencyOnly=false
"""


def test_dynamic_parser_selects_mapped_profile_without_carrier_allowlist():
    obs = parse_sandbox_profile(ISUB_MIXED, "", REGISTRY_HOME)
    assert obs.brand == "US Mobile"
    assert obs.state == PROFILE_STATE_ENABLED
    assert obs.sim_slot_index == 1
    assert obs.registry_home is True
    ok, reason = switch_after_download_reached_enabled(obs)
    assert ok is True
    assert reason is None


def test_dynamic_parser_accepts_unknown_carrier_name():
    obs = parse_sandbox_profile(ISUB_UNKNOWN_CARRIER, "")
    assert obs.brand == "Acme Wireless"
    assert obs.state == PROFILE_STATE_ENABLED
    assert obs.sim_slot_index == 0
    ok, _reason = switch_after_download_reached_enabled(obs)
    assert ok is True
