"""Read-only Android eSIM authorization probe.

Uses public dumpsys/package/role output and the existing companion
``get_esim_status`` command. Never assumes authority. Never logs EID,
IMEI, MSISDN, or activation credentials.
"""
from __future__ import annotations

import re
from typing import Protocol

from domain.esim_capabilities import AndroidAuthorizationSnapshot

COMPANION_PACKAGE = "com.mobirent.companion"
WRITE_EMBEDDED = "android.permission.WRITE_EMBEDDED_SUBSCRIPTIONS"
MANAGED_SUBSCRIPTIONS = "android.permission.MANAGE_DEVICE_POLICY_MANAGED_SUBSCRIPTIONS"
EUICC_FEATURE = "android.hardware.telephony.euicc"
DEVICE_POLICY_ROLES = (
    "android.app.role.DEVICE_POLICY_MANAGEMENT",
    "android.app.role.SYSTEM_DEVICE_POLICY_MANAGER",
    "android.app.role.DEVICE_POLICY_MANAGEMENT_ROLE",
)


class AuthorizationProbe(Protocol):
    def read(self, serial: str | None = None) -> AndroidAuthorizationSnapshot:
        ...


class StaticAuthorizationProbe:
    def __init__(self, snapshot: AndroidAuthorizationSnapshot) -> None:
        self._snapshot = snapshot

    def read(self, serial: str | None = None) -> AndroidAuthorizationSnapshot:
        _ = serial
        return self._snapshot


def parse_device_owner(device_policy_dump: str) -> bool:
    return parse_device_owner_package(device_policy_dump) is not None


def parse_device_owner_package(device_policy_dump: str) -> str | None:
    """Return the Device Owner package, or None if the device is not owned."""
    text = device_policy_dump or ""
    lowered = text.lower()
    if re.search(r"mdeviceowner\s*=\s*null", lowered):
        return None
    if re.search(r"device owner:\s*(none|null)\b", lowered):
        return None
    package = re.search(
        r"device owner:[\s\S]{0,400}?package\s*=\s*([A-Za-z0-9._]+)",
        text,
        re.IGNORECASE,
    )
    if package:
        return package.group(1)
    component = re.search(
        r"device owner:[\s\S]{0,240}componentinfo\{([^/]+)/",
        text,
        re.IGNORECASE,
    )
    if component:
        return component.group(1)
    return None


def parse_profile_owner(device_policy_dump: str) -> bool:
    text = device_policy_dump or ""
    lowered = text.lower()
    if re.search(r"profile owner[^\n]*:\s*(none|null)\b", lowered):
        return False
    if re.search(r"mprofileowners?\s*=\s*(null|\{\}|\[\])", lowered):
        return False
    if re.search(r"profile owner[^\n]*:[\s\S]{0,240}componentinfo\{", lowered):
        return True
    if re.search(r"profile owner[^\n]*:[\s\S]{0,240}package\s*=", lowered):
        return True
    return False


def parse_device_policy_role(role_dump: str) -> bool:
    text = role_dump or ""
    for role in DEVICE_POLICY_ROLES:
        block = _role_block(text, role)
        if block and _role_has_holder(block):
            return True
    return False


def parse_permission_granted(package_dump: str, permission: str) -> bool:
    text = package_dump or ""
    if permission not in text:
        return False
    granted_line = re.search(
        rf"{re.escape(permission)}\s*[:=]\s*granted\s*=\s*true",
        text,
        re.IGNORECASE,
    )
    if granted_line:
        return True
    install = _section_after(text, "install permissions:")
    if install and permission in install.split("runtime permissions:")[0]:
        return True
    granted = _section_after(text, "grantedPermissions:")
    if granted and permission in granted.split("\n\n")[0]:
        return True
    return False


def parse_companion_privileged(package_dump: str) -> bool:
    flags = _first_flags_line(package_dump or "")
    if not flags:
        return False
    return bool(re.search(r"\b(PRIVILEGED|SYSTEM)\b", flags))


def parse_euicc_feature(feature_output: str) -> bool:
    return (feature_output or "").strip().lower() in {"true", "feature: true"}


def parse_lpa_package(econtroller_dump: str) -> str | None:
    text = econtroller_dump or ""
    match = re.search(
        r"(com\.(?:android|google)\.[A-Za-z0-9_.]*euicc[A-Za-z0-9_.]*)",
        text,
    )
    return match.group(1) if match else None


def parse_euicc_enabled_from_econtroller(econtroller_dump: str) -> bool | None:
    text = (econtroller_dump or "").lower()
    if (
        re.search(r"isenabled\s*[:=]\s*true", text)
        or "euicc enabled: true" in text
        or "euiccmanager is enabled: true" in text
    ):
        return True
    if (
        re.search(r"isenabled\s*[:=]\s*false", text)
        or "euicc enabled: false" in text
        or "euiccmanager is enabled: false" in text
    ):
        return False
    return None


def parse_organization_owned(device_policy_dump: str) -> bool:
    return bool(re.search(r"isorganizationowneddevice\s*=\s*true", (device_policy_dump or "").lower()))


def snapshot_from_dumps(
    *,
    device_policy_dump: str = "",
    package_dump: str = "",
    role_dump: str = "",
    feature_output: str = "",
    econtroller_dump: str = "",
    companion_status: dict | None = None,
    real_esim_flag: bool = False,
) -> AndroidAuthorizationSnapshot:
    status = companion_status or {}
    euicc_from_companion = status.get("euicc_enabled")
    euicc_from_controller = parse_euicc_enabled_from_econtroller(econtroller_dump)
    euicc_enabled = False
    if isinstance(euicc_from_companion, bool):
        euicc_enabled = euicc_from_companion
    elif euicc_from_controller is not None:
        euicc_enabled = euicc_from_controller

    write_embedded = bool(status.get("has_write_embedded_subscriptions")) or parse_permission_granted(
        package_dump, WRITE_EMBEDDED
    )
    managed = parse_permission_granted(package_dump, MANAGED_SUBSCRIPTIONS)
    carrier = bool(status.get("has_carrier_privileges"))
    device_owner = parse_device_owner(device_policy_dump) or bool(status.get("device_owner"))
    flag = bool(status.get("real_esim_enabled")) or real_esim_flag
    return AndroidAuthorizationSnapshot(
        euicc_feature=parse_euicc_feature(feature_output),
        euicc_enabled=euicc_enabled,
        lpa_package=parse_lpa_package(econtroller_dump),
        companion_privileged=parse_companion_privileged(package_dump),
        has_write_embedded=write_embedded,
        has_carrier_privileges=carrier,
        device_owner=device_owner,
        profile_owner=parse_profile_owner(device_policy_dump),
        has_managed_subscriptions_permission=managed,
        organization_owned=parse_organization_owned(device_policy_dump),
        real_esim_flag=flag,
    )


class AdbAuthorizationProbe:
    """Live read-only collector. Defaults to no authority on any failure."""

    def __init__(
        self,
        runner,
        companion_client=None,
        *,
        real_esim_flag: bool = False,
        companion_package: str = COMPANION_PACKAGE,
    ) -> None:
        self._runner = runner
        self._companion = companion_client
        self._real_esim_flag = real_esim_flag
        self._package = companion_package

    def read(self, serial: str | None = None) -> AndroidAuthorizationSnapshot:
        if not serial:
            return AndroidAuthorizationSnapshot(real_esim_flag=self._real_esim_flag)
        device_policy = self._shell(serial, ["dumpsys", "device_policy"])
        package_dump = self._shell(serial, ["dumpsys", "package", self._package])
        role_dump = self._shell(serial, ["dumpsys", "role"])
        feature = self._shell(serial, ["pm", "has-feature", EUICC_FEATURE])
        econtroller = self._shell(serial, ["dumpsys", "econtroller"])
        status = self._companion_status(serial)
        snapshot = snapshot_from_dumps(
            device_policy_dump=device_policy,
            package_dump=package_dump,
            role_dump=role_dump,
            feature_output=feature,
            econtroller_dump=econtroller,
            companion_status=status,
            real_esim_flag=self._real_esim_flag,
        )
        if parse_device_policy_role(role_dump) and not (
            snapshot.device_owner or snapshot.profile_owner
        ):
            # Role without an owner is not treated as Device/Profile Owner.
            return snapshot
        return snapshot

    def _shell(self, serial: str, arguments: list[str]) -> str:
        try:
            return self._runner.run(serial, ["shell", *arguments]).stdout
        except Exception:
            return ""

    def _companion_status(self, serial: str) -> dict:
        if self._companion is None:
            return {}
        try:
            response = self._companion.request(serial, {"command": "get_esim_status"})
        except Exception:
            return {}
        if not isinstance(response, dict):
            return {}
        return response


def _first_flags_line(package_dump: str) -> str:
    for line in package_dump.splitlines():
        stripped = line.strip()
        if stripped.startswith("flags=[") or stripped.startswith("pkgFlags=["):
            return stripped
    return ""


def _section_after(text: str, header: str) -> str:
    index = text.find(header)
    if index < 0:
        return ""
    return text[index + len(header) :]


def _role_block(text: str, role: str) -> str:
    index = text.find(role)
    if index < 0:
        return ""
    rest = text[index:]
    next_role = re.search(r"\nRole:", rest[1:])
    return rest[: next_role.start() + 1] if next_role else rest


def _role_has_holder(block: str) -> bool:
    if re.search(r"holders\s*:\s*\[\s*\]", block, re.IGNORECASE):
        return False
    return bool(re.search(r"holders\s*:\s*\[.+\w", block, re.IGNORECASE | re.DOTALL))
