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
RE_JOB = re.compile(rf"^/jobs/({_UUID})$")
RE_FARM_AVAILABLE = re.compile(r"^/farm/slots/available$")
RE_FARM_ASSIGN = re.compile(r"^/farm/slots/(\d{1,2})/assign$")


@dataclass(frozen=True)
class ParsedRoute:
    kind: str
    slot_public_id: str | None = None
    message_id: str | None = None
    job_id: str | None = None
    farm_bay: int | None = None
    action: str | None = None


def parse_route(path: str) -> ParsedRoute | None:
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
    if m := RE_SLOT_ACTION.match(path):
        return ParsedRoute(
            kind="slot_action",
            slot_public_id=m.group(1),
            action=m.group(2),
        )
    return None
