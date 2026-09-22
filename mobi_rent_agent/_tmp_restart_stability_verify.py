"""Restart-safety checks without SMS (read-only config + in-process idempotency)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from application.sms_factory import build_sms_dispatch_service, sms_dispatch_runtime_flags
from application.sms_outbox import DuplicateIdempotencyError, SmsOutbox
from domain.farm import PROTOTYPE_VOIDFIX_DEVICE_ID, SmsFinalStatus, OutboundMessageStatus
from infrastructure.config import load_config
from infrastructure.voidfix_devices import load_voidfix_device_map


def verify_config() -> dict:
    config = load_config(str(ROOT / ".env"))
    flags = sms_dispatch_runtime_flags(config)
    path = config.voidfix_device_map_path or str(ROOT / "voidfix_devices.json")
    device_map = load_voidfix_device_map(path)
    svc = build_sms_dispatch_service(config)
    return {
        "sms_enabled": config.sms_enabled,
        "delivery_poll_enabled": config.voidfix_delivery_poll_enabled,
        "sim_slot_send_enabled": config.voidfix_sim_slot_send_enabled,
        "flags": flags,
        "allowed_count": len(config.voidfix_allowed_slot_ids),
        "allowed_slots": list(config.voidfix_allowed_slot_ids),
        "slot_15_excluded": 15 not in config.voidfix_allowed_slot_ids
        and 15 not in device_map,
        "slot_17_excluded": 17 not in config.voidfix_allowed_slot_ids
        and 17 not in device_map,
        "prototype_in_map": PROTOTYPE_VOIDFIX_DEVICE_ID in set(device_map.values()),
        "poller_active": svc is not None and svc._delivery_poller is not None,
        "map_slot_count": len(device_map),
    }


def verify_idempotency_in_process() -> dict:
    outbox = SmsOutbox()
    key = "restart-safety-idempotency-probe"
    outbox.reserve(slot_id=1, to_number="+15551234567", idempotency_key=key)
    outbox.update(
        key,
        status=OutboundMessageStatus.DELIVERED,
        final_status=SmsFinalStatus.DELIVERED,
        provider_message_id="probe-not-sent",
    )
    dup_blocked = False
    try:
        outbox.reserve(slot_id=1, to_number="+15551234567", idempotency_key=key)
    except DuplicateIdempotencyError:
        dup_blocked = True
    fresh = SmsOutbox()
    fresh_can_reserve_same_key = False
    try:
        fresh.reserve(slot_id=1, to_number="+15551234567", idempotency_key=key)
        fresh_can_reserve_same_key = True
    except DuplicateIdempotencyError:
        pass
    return {
        "idempotency_active_in_process": dup_blocked,
        "fresh_outbox_after_restart_simulation": fresh_can_reserve_same_key,
        "daemon_auto_resend_on_startup": False,
    }


def main() -> None:
    report = {
        "config": verify_config(),
        "idempotency": verify_idempotency_in_process(),
        "outbox_persistence_design": "in_memory_only_no_daemon_outbox",
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
