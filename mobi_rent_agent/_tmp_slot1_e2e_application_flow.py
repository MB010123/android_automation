"""One Slot 1 end-to-end SMS via production SmsDispatchService factory wiring."""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from application.sms_factory import build_sms_dispatch_service
from application.sms_outbox import SmsOutbox
from domain.farm import OutboundMessageStatus, SmsFinalStatus
from infrastructure.config import load_config

SLOT = 1
EXPECTED_VF = "1386"
MESSAGE = "Mobi-Rent Slot 1 controlled E2E application-flow SMS test"


class AuditingOutbox(SmsOutbox):
    def __init__(self) -> None:
        super().__init__()
        self.history: list[dict] = []

    def reserve(self, **kwargs):
        record = super().reserve(**kwargs)
        self.history.append(
            {
                "event": "reserve",
                "status": record.status.value,
                "final_status": record.final_status.value if record.final_status else None,
            }
        )
        return record

    def update(self, idempotency_key: str, **kwargs):
        before = self.get(idempotency_key)
        record = super().update(idempotency_key, **kwargs)
        self.history.append(
            {
                "event": "update",
                "status": record.status.value if record else None,
                "final_status": record.final_status.value if record and record.final_status else None,
                "kwargs": sorted(kwargs.keys()),
            }
        )
        return record


def record_dict(record) -> dict:
    if record is None:
        return {}
    return {
        "status": record.status.value,
        "final_status": record.final_status.value if record.final_status else None,
        "provider_message_id": record.provider_message_id,
        "voidfix_device_id": record.voidfix_device_id,
        "voidfix_sim_slot": record.voidfix_sim_slot,
        "destination_redacted": record.destination_redacted,
        "accepted_at": record.accepted_at,
        "provider_sent_date": record.provider_sent_date,
        "provider_delivered_date": record.provider_delivered_date,
        "provider_error_code": record.provider_error_code,
        "last_polled_at": record.last_polled_at,
    }


def main() -> int:
    config = load_config(str(ROOT / ".env"))
    if not config.voidfix_recipient_allowlist:
        print("FAIL: no authorized recipient in VOIDFIX_RECIPIENT_ALLOWLIST")
        return 2
    recipient = config.voidfix_recipient_allowlist[0]

    outbox = AuditingOutbox()
    service = build_sms_dispatch_service(config, outbox=outbox)
    if service is None:
        print("FAIL: build_sms_dispatch_service returned None")
        return 2

    idem = f"mobi-rent-e2e-{uuid.uuid4().hex[:12]}"
    result = service.send_for_slot(
        SLOT,
        recipient,
        MESSAGE,
        idempotency_key=idem,
        expected_voidfix_device_id=EXPECTED_VF,
    )
    dup = service.send_for_slot(
        SLOT,
        recipient,
        MESSAGE,
        idempotency_key=idem,
        expected_voidfix_device_id=EXPECTED_VF,
    )
    record = outbox.get(idem)

    queued_seen = any(
        h.get("event") == "reserve" and h.get("final_status") == SmsFinalStatus.QUEUED.value
        for h in outbox.history
    )
    accepted_seen = any(
        h.get("final_status") == SmsFinalStatus.ACCEPTED.value for h in outbox.history
    )
    sent_seen = any(h.get("final_status") == SmsFinalStatus.SENT.value for h in outbox.history)

    report = {
        "path": "application.sms_factory.build_sms_dispatch_service -> SmsDispatchService.send_for_slot",
        "slot": SLOT,
        "expected_voidfix_device_id": EXPECTED_VF,
        "recipient_redacted": record.destination_redacted if record else None,
        "idempotency_key": idem,
        "send_success": result.success,
        "send_error": result.error,
        "provider_message_id": result.provider_message_id,
        "duplicate_second_success": dup.success,
        "duplicate_second_error": dup.error,
        "outbox_history": outbox.history,
        "outbox_final": record_dict(record),
        "checks": {},
    }

    report["checks"]["application_request"] = result.success or bool(result.provider_message_id)
    report["checks"]["outbox_queued"] = queued_seen
    report["checks"]["voidfix_accepted"] = (
        record is not None
        and record.final_status in {SmsFinalStatus.ACCEPTED, SmsFinalStatus.SENT, SmsFinalStatus.DELIVERED}
        or accepted_seen
    )
    report["checks"]["provider_message_id_stored"] = bool(record and record.provider_message_id)
    report["checks"]["sent_detected"] = sent_seen or bool(record and record.provider_sent_date)
    report["checks"]["delivered_date_detected"] = bool(record and record.provider_delivered_date)
    report["checks"]["final_delivered"] = (
        record is not None
        and record.status is OutboundMessageStatus.DELIVERED
        and record.final_status is SmsFinalStatus.DELIVERED
    )
    audit_ok = record is not None and all(
        [
            record.provider_message_id,
            record.voidfix_device_id == EXPECTED_VF,
            record.voidfix_sim_slot == 1,
            record.provider_sent_date,
            record.provider_delivered_date,
            record.final_status is SmsFinalStatus.DELIVERED,
            record.last_polled_at is not None,
        ]
    )
    report["checks"]["audit_fields"] = audit_ok
    report["checks"]["duplicate_prevention"] = dup.success is False and "duplicate" in (dup.error or "").lower()
    report["checks"]["slot1_sim2_simslot1"] = (
        record is not None and record.voidfix_device_id == EXPECTED_VF and record.voidfix_sim_slot == 1
    )
    report["sms_count_sent"] = 1 if result.success and result.provider_message_id else 0

    out = ROOT / "_tmp_slot1_e2e_application_flow_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("wrote", out)

    all_pass = all(report["checks"].values()) and report["sms_count_sent"] == 1
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
