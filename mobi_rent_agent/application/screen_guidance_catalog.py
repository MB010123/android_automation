"""Pre-written per-screen instruction bubbles for the customer dashboard.

Maps a clean ``screen_key`` (plus S20 vs Pixel wording) to title / badge /
severity / do_list / dont_list. Never contains device-control commands.
"""
from __future__ import annotations

from typing import Any

SCREEN_KEY_HOME = "home"
SCREEN_KEY_DIALER = "dialer"
SCREEN_KEY_MESSAGES = "messages"
SCREEN_KEY_SETTINGS_MAIN = "settings_main"
SCREEN_KEY_SETTINGS_NETWORK = "settings_network"
SCREEN_KEY_ESIM_QR_FLOW = "esim_qr_flow"
SCREEN_KEY_ABOUT_PHONE = "about_phone"
SCREEN_KEY_BLOCKED_KICK = "blocked_kick"

SCREEN_KEYS = frozenset(
    {
        SCREEN_KEY_HOME,
        SCREEN_KEY_DIALER,
        SCREEN_KEY_MESSAGES,
        SCREEN_KEY_SETTINGS_MAIN,
        SCREEN_KEY_SETTINGS_NETWORK,
        SCREEN_KEY_ESIM_QR_FLOW,
        SCREEN_KEY_ABOUT_PHONE,
        SCREEN_KEY_BLOCKED_KICK,
    }
)

SEVERITY_INFO = "info"
SEVERITY_SUCCESS = "success"
SEVERITY_WARNING = "warning"
DEVICE_FAMILY_S20 = "s20"
DEVICE_FAMILY_PIXEL = "pixel"
OEM_SAMSUNG_ALIAS = "samsung"


def normalize_device_family(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in {DEVICE_FAMILY_S20, "samsung", OEM_SAMSUNG_ALIAS}:
        return DEVICE_FAMILY_S20
    return DEVICE_FAMILY_PIXEL


def guidance_for(
    screen_key: str,
    *,
    device_family: str = DEVICE_FAMILY_PIXEL,
    blocked_reason: str = "",
) -> dict[str, Any]:
    """Return the public ``screen_guidance`` object for one foreground screen."""
    key = str(screen_key or "").strip().lower() or SCREEN_KEY_HOME
    if key not in SCREEN_KEYS:
        key = SCREEN_KEY_HOME
    family = normalize_device_family(device_family)
    reason = str(blocked_reason or "").strip().lower()
    body = _catalog_entry(key, family)
    payload: dict[str, Any] = {
        "screen_key": key,
        "title": body["title"],
        "badge": body["badge"],
        "severity": body["severity"],
        "do_list": list(body["do_list"]),
        "dont_list": list(body["dont_list"]),
        "device_family": family,
    }
    if key == SCREEN_KEY_BLOCKED_KICK and reason:
        payload["blocked_reason"] = reason
    return payload


def _catalog_entry(screen_key: str, family: str) -> dict[str, Any]:
    samsung = family == DEVICE_FAMILY_S20
    if screen_key == SCREEN_KEY_DIALER:
        return {
            "title": "Phone / Calls",
            "badge": "Phone",
            "severity": SEVERITY_INFO,
            "do_list": [
                "Dial any number using the keypad once your eSIM is active.",
                "Check your active SIM line before placing a call.",
            ],
            "dont_list": [
                "Do NOT try to unmute speaker volume—audio is muted on the rack for privacy.",
            ],
        }
    if screen_key == SCREEN_KEY_MESSAGES:
        return {
            "title": "SMS / Text Messages",
            "badge": "SMS",
            "severity": SEVERITY_INFO,
            "do_list": [
                "View incoming 2FA/OTP codes and send SMS texts here.",
                "Tap any conversation to copy or read your verification code.",
            ],
            "dont_list": [
                "Do NOT enable cloud message backup or link a personal Google/Samsung account.",
            ],
        }
    if screen_key == SCREEN_KEY_SETTINGS_MAIN:
        network_step = (
            "Tap **Connections** to set up your eSIM."
            if samsung
            else "Tap **Network & internet** to set up your eSIM."
        )
        other = (
            "On Pixel devices the same menu is **Network & internet**."
            if samsung
            else "On Samsung S20 the same menu is **Connections**."
        )
        return {
            "title": "Settings Menu",
            "badge": "Settings",
            "severity": SEVERITY_INFO,
            "do_list": [
                network_step,
                "Tap **About phone** at the bottom to view IMEI 1, IMEI 2, and EID.",
                other,
            ],
            "dont_list": [
                "Do NOT open Sounds, Security, Accounts, Notifications, or Reset—restricted menus will automatically return you to the Home screen.",
            ],
        }
    if screen_key == SCREEN_KEY_SETTINGS_NETWORK:
        sim_step = (
            "Tap **SIM manager** to view eSIM 1 / eSIM 2."
            if samsung
            else "Tap **SIMs** to view your eSIM profiles."
        )
        return {
            "title": "SIM & Network Setup",
            "badge": "eSIM Setup",
            "severity": SEVERITY_INFO,
            "do_list": [
                sim_step,
                "Toggle the switch next to an eSIM to change active providers (wait 5–10s for the carrier switch popup to finish).",
                "To delete a provider: tap the eSIM name/number (not the switch), then tap Remove / Erase.",
                "Tap Add eSIM to install a new eSIM profile.",
                "Use 'Upload eSIM QR' in the dashboard first, then select 'Scan QR from photo/gallery'.",
                "Keep Wi-Fi turned ON at all times.",
            ],
            "dont_list": [
                "Do NOT erase an active eSIM unless you are replacing it.",
                "Do NOT turn off Wi-Fi—the remote stream requires Wi-Fi to stay connected.",
                "Do NOT disable Wi-Fi or reset network settings.",
            ],
        }
    if screen_key == SCREEN_KEY_ESIM_QR_FLOW:
        return {
            "title": "eSIM QR Scan",
            "badge": "eSIM QR",
            "severity": SEVERITY_SUCCESS,
            "do_list": [
                "Select the QR image at the top of **Recents / Camera** (`esim_qr.png`) that you uploaded from the dashboard.",
                "Wait 15–30 seconds for carrier activation to finish.",
            ],
            "dont_list": [
                "Do NOT close the screen while the eSIM profile is downloading.",
            ],
        }
    if screen_key == SCREEN_KEY_ABOUT_PHONE:
        return {
            "title": "About Phone",
            "badge": "About Phone",
            "severity": SEVERITY_INFO,
            "do_list": [
                "View your **IMEI (slot 1)**, **IMEI (eSIM / slot 2)**, and **EID** here for carrier compatibility checks.",
            ],
            "dont_list": [
                "Do NOT tap Build Number or attempt to enable Developer Options.",
            ],
        }
    if screen_key == SCREEN_KEY_BLOCKED_KICK:
        return {
            "title": "Restricted Screen Blocked",
            "badge": "Restricted",
            "severity": SEVERITY_WARNING,
            "do_list": [
                "Use only **Phone**, **Messages**, **Connections / SIM Manager**, or **About Phone**.",
            ],
            "dont_list": [
                "Security, Sound/Vibrate, Notifications, Account sign-ins, Reset, and non-Phone/SMS apps are locked on rental devices and automatically return to Home.",
            ],
        }
    network_home = (
        "Open **Settings → Connections** to install your eSIM."
        if samsung
        else "Open **Settings → Network & internet** to install your eSIM."
    )
    return {
        "title": "Home Screen",
        "badge": "Home",
        "severity": SEVERITY_INFO,
        "do_list": [
            "Open the **Phone** app to make calls or **Messages** to send/receive SMS & verification codes.",
            network_home,
        ],
        "dont_list": [
            "Do NOT try to open browsers, Camera, Play Store, or other apps—this rental is strictly for Phone & SMS.",
        ],
    }
