"""Four-layer eSIM verification models and a pure verdict evaluator.

No device I/O. No secrets, activation codes, MSISDN, EID, or full IMEI.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from domain.provisioning_state import ActivationVerdict


@dataclass(frozen=True)
class SettingsLpaLayer:
    """Layer A — Settings / LPA. ``settings_profile_visible`` is human-attested."""

    euicc_supported: bool | None = None
    euicc_enabled: bool | None = None
    lpa_package: str | None = None
    lpa_last_profile_list_result: int | None = None
    lpa_last_card_id: int | None = None
    lpa_last_slot_id: int | None = None
    settings_profile_visible: bool | None = None
    settings_carrier_name: str | None = None
    settings_attested_at: str | None = None
    settings_attested_by: str | None = None


@dataclass(frozen=True)
class SubscriptionLayer:
    """Layer B — SubscriptionManager / isub."""

    subscription_present: bool | None = None
    subscription_embedded: bool | None = None
    subscription_id: int | None = None
    default_data_sub_id: int | None = None
    sim_slot_index: int | None = None
    port_index: int | None = None
    card_id: int | None = None
    carrier_name_framework: str | None = None
    embedded_list_empty: bool | None = None


@dataclass(frozen=True)
class TelephonyLayer:
    """Layer C — Telephony / RIL."""

    gsm_sim_state: str | None = None
    sim_state_slot0: str | None = None
    sim_state_slot1: str | None = None
    voice_registered: bool | None = None
    data_registered: bool | None = None
    emergency_only: bool | None = None
    operator_numeric: str | None = None
    radio_access: str | None = None
    data_connection_state: int | None = None


@dataclass(frozen=True)
class ConnectivityLayer:
    """Layer D — actual network. Wi-Fi internet is not cellular proof."""

    wifi_enabled: bool | None = None
    airplane_mode: bool | None = None
    mobile_data_setting: bool | None = None
    cellular_transport_available: bool | None = None
    cellular_ip_present: bool | None = None
    default_route_cellular: bool | None = None
    internet_reachable: bool | None = None
    internet_proves_cellular: bool | None = None
    active_default_network: str | None = None


@dataclass(frozen=True)
class FourLayerVerification:
    settings_lpa: SettingsLpaLayer
    subscription: SubscriptionLayer
    telephony: TelephonyLayer
    connectivity: ConnectivityLayer
    observation_complete: bool = True
    explicit_failure: bool = False
    failure_reason: str | None = None

    def to_log_dict(self) -> dict:
        """Safe log payload: typed layer fields only, no identifiers."""
        return {
            "settings_lpa": asdict(self.settings_lpa),
            "subscription": asdict(self.subscription),
            "telephony": asdict(self.telephony),
            "connectivity": asdict(self.connectivity),
            "observation_complete": self.observation_complete,
            "explicit_failure": self.explicit_failure,
            "failure_reason": self.failure_reason,
        }


def _default_data_sub_valid(sub_id: int | None) -> bool:
    return sub_id is not None and sub_id != -1


def _cellular_path_ok(layer: ConnectivityLayer) -> bool:
    stack = (
        layer.cellular_transport_available is True
        and layer.cellular_ip_present is True
        and layer.default_route_cellular is True
    )
    proven = layer.internet_proves_cellular is True
    if layer.wifi_enabled is True:
        proven = False
    return stack or proven


def _settings_lpa_evidence(layer: SettingsLpaLayer) -> bool:
    return layer.settings_profile_visible is True


def evaluate_activation(verification: FourLayerVerification) -> ActivationVerdict:
    """Pure verdict. No ADB, no network, no profile mutation."""
    if verification.explicit_failure:
        return ActivationVerdict.ACTIVATION_FAILED
    if not verification.observation_complete:
        return ActivationVerdict.VERIFICATION_UNKNOWN

    layer_a = verification.settings_lpa
    layer_b = verification.subscription
    layer_c = verification.telephony
    layer_d = verification.connectivity

    confirmed = (
        layer_a.euicc_enabled is True
        and layer_b.subscription_present is True
        and layer_b.subscription_embedded is True
        and _default_data_sub_valid(layer_b.default_data_sub_id)
        and layer_c.data_registered is True
        and layer_c.emergency_only is False
        and _cellular_path_ok(layer_d)
    )
    if confirmed:
        return ActivationVerdict.ACTIVATION_CONFIRMED

    if _settings_lpa_evidence(layer_a):
        return ActivationVerdict.ACTIVATION_PARTIAL

    incomplete = (
        layer_a.euicc_enabled is None
        or layer_b.subscription_present is None
        or layer_c.data_registered is None
        or layer_d.cellular_transport_available is None
        or (layer_d.wifi_enabled is True and layer_d.internet_reachable is True)
    )
    if incomplete:
        return ActivationVerdict.VERIFICATION_UNKNOWN
    return ActivationVerdict.ACTIVATION_FAILED
