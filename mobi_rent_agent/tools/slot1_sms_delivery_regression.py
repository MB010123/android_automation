"""One Slot 1 SMS send + read-messages delivery poll via SmsDispatchService."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.sms_factory import build_sms_dispatch_service
from application.sms_outbox import SmsOutbox
from domain.farm import OutboundMessageStatus, SmsFinalStatus
from infrastructure.config import load_config

SLOT = 1
VF_DEVICE = "1386"
TO = "+19522287088"
MESSAGE = "Mobi-Rent Slot 1 SMS delivery poll regression"


def main() -> int:
    config = load_config(env_file=str(ROOT / ".env"))
    outbox = SmsOutbox()
    service = build_sms_dispatch_service(config, outbox=outbox)
    if service is None:
        print("SmsDispatchService not configured (check VOIDFIX_* and voidfix_devices.json)")
        return 2

    key_id = f"slot1-delivery-{uuid.uuid4().hex[:8]}"
    result = service.send_for_slot(SLOT, TO, MESSAGE, idempotency_key=key_id)
    record = outbox.get(key_id)
    report = {
        "send_success": result.success,
        "send_error": result.error,
        "provider_message_id": result.provider_message_id,
        "outbox": {
            "status": record.status.value if record else None,
            "final_status": record.final_status.value if record and record.final_status else None,
            "provider_message_id": record.provider_message_id if record else None,
            "voidfix_device_id": record.voidfix_device_id if record else None,
            "voidfix_sim_slot": record.voidfix_sim_slot if record else None,
            "destination_redacted": record.destination_redacted if record else None,
            "accepted_at": record.accepted_at if record else None,
            "provider_sent_date": record.provider_sent_date if record else None,
            "provider_delivered_date": record.provider_delivered_date if record else None,
            "provider_error_code": record.provider_error_code if record else None,
            "last_polled_at": record.last_polled_at if record else None,
        },
    }
    out = ROOT / "_tmp_slot1_delivery_regression.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("wrote", out)

    ok = (
        record is not None
        and record.final_status is SmsFinalStatus.DELIVERED
        and record.status is OutboundMessageStatus.DELIVERED
        and record.provider_delivered_date
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
