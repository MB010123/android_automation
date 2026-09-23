"""Configurable inbound-webhook → outbound SMS reply rules (VPS-side policy only)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WebhookReplyRule:
    inbound_slot: int
    sender_slot: int
    reply_to_slot: int


@dataclass(frozen=True)
class WebhookReplyPolicy:
    enabled: bool
    rules: tuple[WebhookReplyRule, ...]


def load_webhook_reply_policy() -> WebhookReplyPolicy:
    enabled = _read_bool("WEBHOOK_AUTO_REPLY_ENABLED", False)
    raw = os.getenv("WEBHOOK_AUTO_REPLY_RULES", "").strip()
    if not raw:
        return WebhookReplyPolicy(enabled=enabled, rules=())
    rules: list[WebhookReplyRule] = []
    if raw.startswith("{"):
        data = json.loads(raw)
        for item in data.get("rules", []):
            rules.append(
                WebhookReplyRule(
                    inbound_slot=int(item["inbound_slot"]),
                    sender_slot=int(item["sender_slot"]),
                    reply_to_slot=int(item.get("reply_to_slot", item["inbound_slot"])),
                )
            )
        return WebhookReplyPolicy(enabled=enabled, rules=tuple(rules))
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        # format: inbound:2->send:1->to:2
        inbound = sender = reply_to = None
        for seg in part.split("->"):
            seg = seg.strip()
            if seg.startswith("inbound:"):
                inbound = int(seg.split(":", 1)[1])
            elif seg.startswith("send:"):
                sender = int(seg.split(":", 1)[1])
            elif seg.startswith("to:"):
                reply_to = int(seg.split(":", 1)[1])
        if inbound is None or sender is None:
            raise ValueError(f"invalid WEBHOOK_AUTO_REPLY_RULES segment: {part}")
        rules.append(
            WebhookReplyRule(
                inbound_slot=inbound,
                sender_slot=sender,
                reply_to_slot=reply_to if reply_to is not None else inbound,
            )
        )
    return WebhookReplyPolicy(enabled=enabled, rules=tuple(rules))


def rule_for_inbound_slot(policy: WebhookReplyPolicy, inbound_slot: int | None) -> WebhookReplyRule | None:
    if not policy.enabled or inbound_slot is None:
        return None
    for rule in policy.rules:
        if rule.inbound_slot == int(inbound_slot):
            return rule
    return None


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")
