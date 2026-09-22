"""Read-only four-layer activation observation over ADB.

Never logs EID, IMEI, MSISDN, or activation codes. Wi-Fi internet is not
treated as cellular proof.
"""
from __future__ import annotations

import re

from domain.verification import (
    ConnectivityLayer,
    FourLayerVerification,
    SettingsLpaLayer,
    SubscriptionLayer,
    TelephonyLayer,
)


def collect_four_layer_verification(runner, serial: str, companion_status: dict | None = None) -> FourLayerVerification:
    status = companion_status or {}
    isub = _shell(runner, serial, ["dumpsys", "isub"])
    registry = _shell(runner, serial, ["dumpsys", "telephony.registry"])
    wifi = _shell(runner, serial, ["dumpsys", "wifi"])
    connectivity = _shell(runner, serial, ["dumpsys", "connectivity"])
    gsm_state = _shell(runner, serial, ["getprop", "gsm.sim.state"])
    wifi_on = _wifi_enabled(wifi)
    cellular_iface = _cellular_iface_up(runner, serial)
    default_cellular = _default_route_cellular(connectivity)
    data_registered = _data_registered(registry)
    emergency = _emergency_only(registry)
    embedded_present, default_data, embedded_empty = _subscription_bits(isub)
    euicc_enabled = status.get("euicc_enabled")
    if not isinstance(euicc_enabled, bool):
        euicc_enabled = True if "EuiccManager is enabled: true" in _shell(runner, serial, ["dumpsys", "econtroller"]) else None
    return FourLayerVerification(
        settings_lpa=SettingsLpaLayer(
            euicc_supported=True,
            euicc_enabled=euicc_enabled,
            lpa_package=status.get("lpa_package") if isinstance(status.get("lpa_package"), str) else None,
            settings_profile_visible=None,
        ),
        subscription=SubscriptionLayer(
            subscription_present=embedded_present,
            subscription_embedded=embedded_present,
            default_data_sub_id=default_data,
            embedded_list_empty=embedded_empty,
        ),
        telephony=TelephonyLayer(
            gsm_sim_state=gsm_state or None,
            data_registered=data_registered,
            emergency_only=emergency,
        ),
        connectivity=ConnectivityLayer(
            wifi_enabled=wifi_on,
            cellular_transport_available=cellular_iface,
            cellular_ip_present=cellular_iface,
            default_route_cellular=default_cellular,
            internet_reachable=None,
            internet_proves_cellular=False,
            active_default_network="CELLULAR" if default_cellular else ("WIFI" if wifi_on else "none"),
        ),
        observation_complete=True,
    )


def _shell(runner, serial: str, arguments: list[str]) -> str:
    try:
        return runner.run(serial, ["shell", *arguments]).stdout
    except Exception:
        return ""


def _subscription_bits(isub: str) -> tuple[bool, int | None, bool]:
    text = isub or ""
    embedded_empty = bool(re.search(r"Embedded subscriptions:\s*\[\s*\]", text)) or "mEmbeddedSubscriptions: []" in text
    if "Embedded subscriptions: []" in text:
        embedded_empty = True
    present = (not embedded_empty) and bool(re.search(r"Embedded subscriptions:\s*\[(?!\s*\])", text))
    default_ids = [int(m) for m in re.findall(r"(?:mDefaultDataSubId|defaultDataSubId)=(-?\d+)", text)]
    default_data = default_ids[-1] if default_ids else None
    if default_data == -1:
        present = False
        embedded_empty = True
    return present, default_data, embedded_empty


def _data_registered(registry: str) -> bool:
    return bool(
        re.search(r"dataRegState=0\b", registry or "")
        or re.search(r"mDataConnectionState=2\b", registry or "")
    )


def _emergency_only(registry: str) -> bool:
    return "emergencyOnly=true" in (registry or "").lower() or "EMERGENCY_ONLY" in (registry or "")


def _wifi_enabled(wifi: str) -> bool:
    return bool(re.search(r"Wi-Fi is enabled", wifi or "", re.I) or re.search(r"mWifiEnabled:\s*true", wifi or "", re.I))


def _cellular_iface_up(runner, serial: str) -> bool:
    ip_addr = _shell(runner, serial, ["ip", "-o", "addr"])
    return bool(re.search(r"\brmnet(?:_data)?\d+.*\binet ", ip_addr))


def _default_route_cellular(connectivity: str) -> bool:
    text = connectivity or ""
    has_cellular = "Transports: CELLULAR" in text or "TRANSPORT_CELLULAR" in text
    has_internet = "INTERNET" in text and has_cellular
    wifi_default = bool(re.search(r"isDefault=true[\s\S]{0,80}Transports: WIFI", text)) or bool(
        re.search(r"Transports: WIFI[\s\S]{0,80}isDefault=true", text)
    )
    if wifi_default:
        return False
    return has_internet
