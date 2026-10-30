"""Samsung One UI launch/recovery helpers. Never implicit Settings intents.

Game Launcher (`com.samsung.android.game.gamehome`) registers for generic
`android.settings.*` actions on some One UI builds and steals the Settings
button plus eSIM setup. Farm code always starts explicit components on
Samsung and disables those game packages when a bay is prepared.
"""
from __future__ import annotations

from typing import Any

SAMSUNG_S20_SERIAL = "RFCNB056WEV"
SAMSUNG_S20_SLOT_ID = 21

SAMSUNG_GAME_PACKAGES = (
    "com.samsung.android.game.gamehome",
    "com.samsung.android.game.gametools",
    "com.samsung.android.game.gos",
    "com.enhance.gameservice",
)

ANDROID_SETTINGS_COMPONENT = "com.android.settings/.Settings"
SAMSUNG_SETTINGS_HOMEPAGE_COMPONENT = (
    "com.android.settings/.homepage.SettingsHomepageActivity"
)
NETWORK_DASHBOARD_COMPONENT = "com.android.settings/.Settings$NetworkDashboardActivity"
SAMSUNG_SIM_MGR_COMPONENT = (
    "com.samsung.android.app.telephonyui/"
    ".netsettings.ui.simcardmanager.SimCardMgrActivity"
)
DEVICE_INFO_COMPONENT = "com.android.settings/.Settings$MyDeviceInfoActivity"


def is_samsung_device(
    *,
    slot_id: int = 0,
    serial: str = "",
    manufacturer: str = "",
) -> bool:
    if int(slot_id or 0) == SAMSUNG_S20_SLOT_ID:
        return True
    if str(serial or "").strip().upper() == SAMSUNG_S20_SERIAL:
        return True
    brand = str(manufacturer or "").strip().lower()
    return brand == "samsung" or brand.startswith("samsung")


def package_from_component(activity: str | None) -> str:
    text = str(activity or "").strip()
    if "/" not in text:
        return text
    return text.split("/", 1)[0].strip()


def is_samsung_game_package(package: str | None) -> bool:
    return str(package or "").strip() in SAMSUNG_GAME_PACKAGES


def ui_shows_samsung_game(ui_xml: str | None) -> bool:
    lowered = (ui_xml or "").lower()
    return any(package.lower() in lowered for package in SAMSUNG_GAME_PACKAGES)


def disable_samsung_game_commands() -> tuple[str, ...]:
    commands: list[str] = []
    for package in SAMSUNG_GAME_PACKAGES:
        commands.append(f"pm disable-user --user 0 {package}")
        commands.append(f"am force-stop {package}")
    return tuple(commands)


def samsung_sim_manager_args() -> list[str]:
    return [
        "am",
        "start",
        "--activity-clear-top",
        "-n",
        SAMSUNG_SIM_MGR_COMPONENT,
        "-e",
        "rootKey",
        "SIMCARD_ESIM_ADD_MOBILE_PLAN",
    ]


def samsung_settings_args() -> list[str]:
    """Bring the One UI Settings homepage to the front.

    ``FLAG_ACTIVITY_CLEAR_TASK`` without ``FLAG_ACTIVITY_NEW_TASK`` (0x10000000)
    is ignored on Android, so a previous force-stop leaves the S20 on Home.
    Game Launcher steals generic ``android.settings.SETTINGS`` intents — keep
    the explicit component.
    """
    return [
        "am",
        "start",
        "-W",
        "-n",
        ANDROID_SETTINGS_COMPONENT,
        "--activity-clear-top",
        "-f",
        "0x10000000",
    ]


def samsung_settings_homepage_args() -> list[str]:
    return [
        "am",
        "start",
        "-W",
        "-n",
        SAMSUNG_SETTINGS_HOMEPAGE_COMPONENT,
        "--activity-clear-top",
        "-f",
        "0x10000000",
    ]


def pixel_settings_args() -> list[str]:
    return [
        "am",
        "start",
        "-W",
        "-a",
        "android.settings.SETTINGS",
        "--activity-clear-top",
        "-f",
        "0x10000000",
    ]


def samsung_network_args() -> list[str]:
    return ["am", "start", "-n", NETWORK_DASHBOARD_COMPONENT, "--activity-clear-task"]


def samsung_device_info_args() -> list[str]:
    return ["am", "start", "-n", DEVICE_INFO_COMPONENT, "--activity-clear-task"]


def hijack_escape_args(activity: str | None = None) -> list[list[str]]:
    """Force-stop Game Launcher (and the current hijack package), then Home."""
    commands: list[list[str]] = []
    seen: set[str] = set()
    current = package_from_component(activity)
    for package in (*SAMSUNG_GAME_PACKAGES, current):
        pkg = str(package or "").strip()
        if not pkg or pkg in seen:
            continue
        seen.add(pkg)
        commands.append(["am", "force-stop", pkg])
    commands.append(["input", "keyevent", "KEYCODE_HOME"])
    return commands


def samsung_setup_relaunch_args(*, phase: str) -> list[str]:
    if str(phase or "").strip().lower() == "voidfix":
        return samsung_settings_args()
    return samsung_sim_manager_args()


def escape_samsung_game_hijack(runner: Any, serial: str, activity: str | None = None) -> None:
    """Best-effort force-stop + Home. Never raises."""
    if runner is None:
        return
    for args in hijack_escape_args(activity):
        try:
            runner.run(serial, ["shell", *args])
        except Exception:
            continue
