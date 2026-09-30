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
RE_FARM_AVAILABLE = re.compile(r"^/farm/slots/available$")
RE_FARM_ASSIGN = re.compile(r"^/farm/slots/(\d{1,2})/assign$")
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
    rf"^/rentals/({_UUID})/remote-access/(revoke|release|device-status|reboot|prepare-esim|activation-status)$"
)
REMOTE_ACCESS_KINDS = frozenset({"remote_access", "remote_access_action"})

PUBLIC_AUTH_POST = frozenset(
    {"auth_signup", "auth_login", "auth_forgot", "auth_reset", "auth_verify"}
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
FARM_SERVICE_ONLY = frozenset({"farm_available", "farm_assign"})


@dataclass(frozen=True)
class ParsedRoute:
    kind: str
    slot_public_id: str | None = None
    message_id: str | None = None
    job_id: str | None = None
    farm_bay: int | None = None
    action: str | None = None
    rental_id: str | None = None


def parse_route(path: str) -> ParsedRoute | None:
    if m := RE_REMOTE_ACCESS.match(path):
        return ParsedRoute(kind="remote_access", rental_id=m.group(1))
    if m := RE_REMOTE_ACCESS_ACTION.match(path):
        return ParsedRoute(kind="remote_access_action", rental_id=m.group(1), action=m.group(2))
    if m := RE_FARM_AVAILABLE.match(path):
        return ParsedRoute(kind="farm_available")
    if m := RE_FARM_ASSIGN.match(path):
        return ParsedRoute(kind="farm_assign", farm_bay=int(m.group(1)))
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
