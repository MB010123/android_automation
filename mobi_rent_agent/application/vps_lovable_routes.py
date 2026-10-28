"""Path parsing for Lovable-facing VPS API routes."""
from __future__ import annotations

import re
from dataclasses import dataclass

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

RE_SLOT_SMS_SEND = re.compile(rf"^/slots/({_UUID})/sms/send$")
RE_MESSAGE = re.compile(rf"^/messages/({_UUID})$")
RE_SLOT_MESSAGES = re.compile(rf"^/slots/({_UUID})/messages$")
RE_SLOT_ACTION = re.compile(rf"^/slots/({_UUID})/actions/([a-z_]+)$")
RE_SLOT_EVENTS = re.compile(rf"^/slots/({_UUID})/events$")
RE_SLOT_STATUS = re.compile(rf"^/slots/({_UUID})/status$")
RE_SLOT_ESIM = re.compile(rf"^/slots/({_UUID})/esim$")
RE_SLOT_DETAIL = re.compile(rf"^/slots/({_UUID})$")
RE_SLOTS_LIST = re.compile(r"^/slots$")
RE_JOB = re.compile(rf"^/jobs/({_UUID})$")
RE_FARM_SLOTS = re.compile(r"^/farm/slots$")
RE_FARM_AVAILABLE = re.compile(r"^/farm/slots/available$")
RE_FARM_ASSIGN = re.compile(r"^/farm/slots/(\d{1,2})/assign$")
RE_FARM_AUTO_CLEANUP = re.compile(r"^/farm/slots/(\d{1,2})/auto-cleanup$")
RE_AUTH_SIGNUP = re.compile(r"^/auth/signup$")
RE_AUTH_LOGIN = re.compile(r"^/auth/login$")
RE_AUTH_LOGOUT = re.compile(r"^/auth/logout$")
RE_AUTH_ME = re.compile(r"^/auth/me$")
RE_AUTH_SESSION = re.compile(r"^/auth/session$")
RE_AUTH_FORGOT = re.compile(r"^/auth/forgot-password$")
RE_AUTH_RESET = re.compile(r"^/auth/reset-password$")
RE_AUTH_VERIFY = re.compile(r"^/auth/verify-email$")
RE_AUTH_RESEND = re.compile(r"^/auth/resend-verification$")
# Remote-access POC (Slot 1). Keyed by rental only: the browser never names
# a slot or device; the backend derives both from the rental row.
RE_REMOTE_ACCESS = re.compile(rf"^/rentals/({_UUID})/remote-access$")
RE_REMOTE_ACCESS_ACTION = re.compile(
    rf"^/rentals/({_UUID})/remote-access/"
    r"(revoke|release|device-status|reboot|prepare-esim|activation-status|control|complete|stream|reconnect-cellular|troubleshoot)$"
)
RE_ESIM_UPLOAD = re.compile(rf"^/rentals/({_UUID})/esim/upload$")
RE_RENTAL_END = re.compile(rf"^/rentals/({_UUID})/end$")
RE_RENTAL_CANCEL = re.compile(rf"^/rentals/({_UUID})/cancel$")
RE_FARM_CLEANUP_VERIFIED = re.compile(r"^/farm/slots/(\d{1,2})/cleanup-verified$")
RE_FARM_ACCESS_MODE = re.compile(r"^/farm/access-mode$")
RE_FARM_SLOT_OP = re.compile(
    r"^/farm/slots/(\d{1,2})/"
    r"(reboot|airplane-mode|about-phone-screenshot|disable-rental|enable-rental|force-available|clean|access-mode)$"
)
RE_DEVICES_AIRPLANE = re.compile(r"^/devices/(\d{1,2})/airplane-mode$")
# Admin live view. The device reference may be a farm bay number (1–32), a
# slot public UUID (Supabase public.slots id), or an ADB serial; the admin
# service resolves it. Separate from customer /rentals/.../remote-access.
_DEVICE_REF = r"[0-9a-zA-Z\-]{1,64}"
RE_ADMIN_DEVICE_OP = re.compile(
    rf"^/admin/devices/({_DEVICE_REF})/"
    r"(reboot|airplane-mode|about-phone-screenshot|disable-rental|enable-rental|force-available|cleanup-verified|clean|access-mode)$"
)
RE_ADMIN_SLOT_CLEAN = re.compile(rf"^/admin/slots/({_DEVICE_REF})/clean$")
RE_ADMIN_LIVE_ACCESS = re.compile(rf"^/admin/devices/({_DEVICE_REF})/live-access$")
RE_ADMIN_LIVE_ACCESS_ACTION = re.compile(
    rf"^/admin/devices/({_DEVICE_REF})/live-access/(stream|control|close|airplane-mode)$"
)
RE_ADMIN_LIVE_AIRPLANE = re.compile(rf"^/admin/live-access/({_DEVICE_REF})/airplane-mode$")
RE_WHMCS_WEBHOOK = re.compile(r"^/api/webhooks/whmcs$")
RE_WHMCS_SSO = re.compile(r"^/api/public/auth/whmcs-sso$")
RE_BILLING_CONFIG = re.compile(r"^/billing/config$")
RE_BILLING_CREDITS = re.compile(r"^/billing/credits$")
REMOTE_ACCESS_KINDS = frozenset({"remote_access", "remote_access_action"})
ADMIN_LIVE_ACCESS_KINDS = frozenset(
    {"admin_live_access", "admin_live_access_action"}
)

PUBLIC_AUTH_POST = frozenset(
    {
        "auth_signup",
        "auth_login",
        "auth_forgot",
        "auth_reset",
        "auth_verify",
        "whmcs_sso",
    }
)
USER_OWNED_KINDS = frozenset(
    {
        "slot_status",
        "slot_detail",
        "slot_events",
        "slot_messages",
        "slot_sms_send",
        "slot_action",
        "slot_esim",
    }
)
FARM_SERVICE_ONLY = frozenset({"farm_available", "farm_assign", "rental_end", "cleanup_verified"})
_FARM_SLOT_KIND = {
    "reboot": "farm_reboot",
    "airplane-mode": "farm_airplane_mode",
    "about-phone-screenshot": "farm_about_phone",
    "disable-rental": "farm_disable_rental",
    "enable-rental": "farm_enable_rental",
    "force-available": "farm_force_available",
    "clean": "farm_clean",
    "access-mode": "farm_access_mode_slot",
}
_ADMIN_DEVICE_KIND = {
    "reboot": "admin_device_reboot",
    "airplane-mode": "admin_device_airplane",
    "about-phone-screenshot": "admin_device_about_phone",
    "disable-rental": "admin_device_disable",
    "enable-rental": "admin_device_enable",
    "force-available": "admin_device_force_available",
    "cleanup-verified": "admin_device_cleanup_verified",
    "clean": "admin_device_clean",
    "access-mode": "admin_device_access_mode",
}
FARM_OR_ADMIN_KINDS = frozenset(
    {
        "farm_auto_cleanup",
        "farm_clean",
        "admin_device_clean",
        "admin_slot_clean",
        "farm_reboot",
        "farm_airplane_mode",
        "farm_about_phone",
        "farm_disable_rental",
        "farm_enable_rental",
        "farm_force_available",
        "admin_device_reboot",
        "admin_device_airplane",
        "admin_device_about_phone",
        "admin_device_disable",
        "admin_device_enable",
        "admin_device_force_available",
        "admin_device_cleanup_verified",
        "admin_live_airplane",
        "devices_airplane_mode",
        "farm_access_mode",
        "farm_access_mode_slot",
        "admin_device_access_mode",
    }
)


@dataclass(frozen=True)
class ParsedRoute:
    kind: str
    slot_public_id: str | None = None
    message_id: str | None = None
    job_id: str | None = None
    farm_bay: int | None = None
    action: str | None = None
    rental_id: str | None = None
    # Raw /admin/devices/{ref} path segment: bay number, slot UUID, or serial.
    device_ref: str | None = None


def parse_route(path: str) -> ParsedRoute | None:
    if m := RE_ESIM_UPLOAD.match(path):
        return ParsedRoute(kind="esim_qr_upload", rental_id=m.group(1))
    if m := RE_RENTAL_CANCEL.match(path):
        return ParsedRoute(kind="rental_cancel", rental_id=m.group(1))
    if m := RE_RENTAL_END.match(path):
        return ParsedRoute(kind="rental_end", rental_id=m.group(1))
    if m := RE_REMOTE_ACCESS.match(path):
        return ParsedRoute(kind="remote_access", rental_id=m.group(1))
    if m := RE_REMOTE_ACCESS_ACTION.match(path):
        return ParsedRoute(kind="remote_access_action", rental_id=m.group(1), action=m.group(2))
    if RE_FARM_ACCESS_MODE.match(path):
        return ParsedRoute(kind="farm_access_mode")
    if RE_FARM_SLOTS.match(path):
        return ParsedRoute(kind="farm_slots")
    if m := RE_FARM_AVAILABLE.match(path):
        return ParsedRoute(kind="farm_available")
    if m := RE_FARM_AUTO_CLEANUP.match(path):
        return ParsedRoute(kind="farm_auto_cleanup", farm_bay=int(m.group(1)))
    if m := RE_ADMIN_SLOT_CLEAN.match(path):
        ref = m.group(1)
        return ParsedRoute(
            kind="admin_slot_clean",
            farm_bay=int(ref) if ref.isdigit() else None,
            action="clean",
            device_ref=ref,
        )
    if m := RE_FARM_ASSIGN.match(path):
        return ParsedRoute(kind="farm_assign", farm_bay=int(m.group(1)))
    if m := RE_FARM_CLEANUP_VERIFIED.match(path):
        return ParsedRoute(kind="cleanup_verified", farm_bay=int(m.group(1)))
    if m := RE_FARM_SLOT_OP.match(path):
        return ParsedRoute(
            kind=_FARM_SLOT_KIND[m.group(2)],
            farm_bay=int(m.group(1)),
            action=m.group(2),
        )
    if m := RE_DEVICES_AIRPLANE.match(path):
        return ParsedRoute(kind="devices_airplane_mode", farm_bay=int(m.group(1)))
    if m := RE_ADMIN_LIVE_AIRPLANE.match(path):
        ref = m.group(1)
        return ParsedRoute(
            kind="admin_live_airplane",
            farm_bay=int(ref) if ref.isdigit() else None,
            action="airplane-mode",
            device_ref=ref,
        )
    if m := RE_ADMIN_LIVE_ACCESS.match(path):
        ref = m.group(1)
        return ParsedRoute(
            kind="admin_live_access",
            farm_bay=int(ref) if ref.isdigit() else None,
            device_ref=ref,
        )
    if m := RE_ADMIN_LIVE_ACCESS_ACTION.match(path):
        ref = m.group(1)
        action = m.group(2)
        if action == "airplane-mode":
            return ParsedRoute(
                kind="admin_live_airplane",
                farm_bay=int(ref) if ref.isdigit() else None,
                action=action,
                device_ref=ref,
            )
        return ParsedRoute(
            kind="admin_live_access_action",
            farm_bay=int(ref) if ref.isdigit() else None,
            action=action,
            device_ref=ref,
        )
    if m := RE_ADMIN_DEVICE_OP.match(path):
        ref = m.group(1)
        return ParsedRoute(
            kind=_ADMIN_DEVICE_KIND[m.group(2)],
            farm_bay=int(ref) if ref.isdigit() else None,
            action=m.group(2),
            device_ref=ref,
        )
    if RE_WHMCS_WEBHOOK.match(path):
        return ParsedRoute(kind="whmcs_webhook")
    if RE_WHMCS_SSO.match(path):
        return ParsedRoute(kind="whmcs_sso")
    if RE_BILLING_CONFIG.match(path):
        return ParsedRoute(kind="billing_config")
    if RE_BILLING_CREDITS.match(path):
        return ParsedRoute(kind="billing_credits")
    if m := RE_JOB.match(path):
        return ParsedRoute(kind="job", job_id=m.group(1))
    if m := RE_SLOT_SMS_SEND.match(path):
        return ParsedRoute(kind="slot_sms_send", slot_public_id=m.group(1))
    if m := RE_MESSAGE.match(path):
        return ParsedRoute(kind="message", message_id=m.group(1))
    if m := RE_SLOT_MESSAGES.match(path):
        return ParsedRoute(kind="slot_messages", slot_public_id=m.group(1))
    if m := RE_SLOT_EVENTS.match(path):
        return ParsedRoute(kind="slot_events", slot_public_id=m.group(1))
    if m := RE_SLOT_STATUS.match(path):
        return ParsedRoute(kind="slot_status", slot_public_id=m.group(1))
    if m := RE_SLOT_ESIM.match(path):
        return ParsedRoute(kind="slot_esim", slot_public_id=m.group(1))
    if m := RE_SLOT_ACTION.match(path):
        return ParsedRoute(
            kind="slot_action",
            slot_public_id=m.group(1),
            action=m.group(2),
        )
    if m := RE_SLOT_DETAIL.match(path):
        return ParsedRoute(kind="slot_detail", slot_public_id=m.group(1))
    if RE_SLOTS_LIST.match(path):
        return ParsedRoute(kind="slots_list")
    if RE_AUTH_SIGNUP.match(path):
        return ParsedRoute(kind="auth_signup")
    if RE_AUTH_LOGIN.match(path):
        return ParsedRoute(kind="auth_login")
    if RE_AUTH_LOGOUT.match(path):
        return ParsedRoute(kind="auth_logout")
    if RE_AUTH_ME.match(path):
        return ParsedRoute(kind="auth_me")
    if RE_AUTH_SESSION.match(path):
        return ParsedRoute(kind="auth_session")
    if RE_AUTH_FORGOT.match(path):
        return ParsedRoute(kind="auth_forgot")
    if RE_AUTH_RESET.match(path):
        return ParsedRoute(kind="auth_reset")
    if RE_AUTH_VERIFY.match(path):
        return ParsedRoute(kind="auth_verify")
    if RE_AUTH_RESEND.match(path):
        return ParsedRoute(kind="auth_resend")
    return None
