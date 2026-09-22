"""Parse VoidFix inbound webhook HTTP bodies (shared by VPS and legacy listener)."""
from __future__ import annotations

import json
from urllib.parse import parse_qs


class WebhookBodyError(ValueError):
    """POST body could not be parsed into an ingest_inbound payload."""


def parse_inbound_http_body(raw: bytes, content_type: str | None) -> object:
    """Parse VoidFix inbound webhook bodies (JSON or form messages= JSON)."""
    if not raw.strip():
        return {}

    ct = (content_type or "").split(";")[0].strip().lower()

    if ct == "application/x-www-form-urlencoded":
        form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        messages_values = form.get("messages") or []
        if not messages_values or not str(messages_values[0]).strip():
            raise WebhookBodyError("form body missing messages field")
        try:
            return json.loads(messages_values[0])
        except json.JSONDecodeError as exc:
            raise WebhookBodyError("messages field is not valid JSON") from exc

    if ct in {"", "application/json"}:
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WebhookBodyError("body must be valid JSON") from exc

    raise WebhookBodyError(f"unsupported Content-Type: {content_type}")


def slot_for_voidfix_device(device_id: str | None, device_map: dict[int, str]) -> int | None:
    if not device_id:
        return None
    target = str(device_id).strip()
    for slot_id, vf_id in device_map.items():
        if str(vf_id) == target:
            return int(slot_id)
    return None
