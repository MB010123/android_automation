"""Operational backup/restore validation for logs/sms_outbox.sqlite (no SMS)."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from application.sms_factory import DEFAULT_SMS_OUTBOX_DB, create_durable_sms_outbox
from application.sms_outbox import DuplicateIdempotencyError, SmsOutbox
from application.sms_service import SmsDispatchService
from domain.farm import OutboundMessageStatus, SmsFinalStatus
from domain.models import InboundSms
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.sms_outbox_store import SCHEMA_VERSION, SqliteSmsOutboxStore
from infrastructure.voidfix_delivery import DeliveryPollResult, VoidFixMessageSnapshot


class _NoSendGateway:
    def send(self, *args, **kwargs):
        raise AssertionError("gateway.send must not run during restore recovery test")

    def fetch_inbound(self):
        return []

    def ingest_inbound(self, payload):
        return [InboundSms(from_number="+1", message="x")]


class _FakePoller:
    def __init__(self):
        self.calls = 0

    def poll_until_terminal(self, provider_message_id, **kwargs):
        self.calls += 1
        snap = VoidFixMessageSnapshot(
            message_id=str(provider_message_id),
            status="Sent",
            device_id="1386",
            sim_slot=1,
            destination="+15551234567",
            sent_date="2026-09-18T20:00:01+0000",
            delivered_date="2026-09-18T20:00:09+0000",
            error_code="-1",
            raw={"deliveredDate": "2026-09-18T20:00:09+0000"},
        )
        on_poll = kwargs.get("on_poll")
        if on_poll:
            on_poll(snap)
        return DeliveryPollResult(final_status=SmsFinalStatus.DELIVERED, snapshot=snap)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_sample_records(db_path: Path) -> None:
    store = SqliteSmsOutboxStore(db_path)
    outbox = SmsOutbox(store=store)
    outbox.reserve(slot_id=1, to_number="+15551234567", idempotency_key="bk-delivered")
    outbox.update(
        "bk-delivered",
        status=OutboundMessageStatus.DELIVERED,
        provider_message_id="99001",
        voidfix_device_id="1386",
        voidfix_sim_slot=1,
        accepted_at=100.0,
        provider_sent_date="2026-09-18T19:00:01+0000",
        provider_delivered_date="2026-09-18T19:00:08+0000",
        provider_error_code="-1",
        final_status=SmsFinalStatus.DELIVERED,
        last_polled_at=108.0,
    )
    outbox.reserve(slot_id=1, to_number="+15551234567", idempotency_key="bk-inflight")
    outbox.update(
        "bk-inflight",
        status=OutboundMessageStatus.SENT,
        provider_message_id="99002",
        voidfix_device_id="1386",
        voidfix_sim_slot=1,
        accepted_at=200.0,
        provider_sent_date="2026-09-18T19:05:01+0000",
        final_status=SmsFinalStatus.SENT,
        last_polled_at=205.0,
    )
    store.close()


def main() -> int:
    report: dict = {"checks": {}}
    live = DEFAULT_SMS_OUTBOX_DB.resolve()

    # Materialize production path if missing (same as factory startup).
    if not live.exists():
        create_durable_sms_outbox(live)
    report["live_db_path"] = str(live)
    report["checks"]["live_sqlite_outbox"] = live.exists() and live.name == "sms_outbox.sqlite"

    live_hash_before = _sha256(live)
    live_size_before = live.stat().st_size
    report["live_hash_before"] = live_hash_before

    backup_dir = ROOT / "logs" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"sms_outbox-{stamp}.sqlite"
    shutil.copy2(live, backup_path)
    report["backup_path"] = str(backup_path)
    report["checks"]["backup"] = backup_path.exists() and _sha256(backup_path) == live_hash_before

    # Work on an isolated clone; never mutate the live file after baseline hash.
    work = ROOT / "logs" / "backups" / f"sms_outbox-restore-work-{stamp}.sqlite"
    restored = ROOT / "logs" / "backups" / f"sms_outbox-restored-{stamp}.sqlite"
    shutil.copy2(live, work)
    _seed_sample_records(work)
    work_backup = backup_dir / f"sms_outbox-work-{stamp}.sqlite.bak"
    shutil.copy2(work, work_backup)
    shutil.copy2(work_backup, restored)

    store = SqliteSmsOutboxStore(restored)
    outbox = SmsOutbox(store=store)
    delivered = outbox.get("bk-delivered")
    inflight = outbox.get("bk-inflight")
    store.close()

    integrity = (
        delivered is not None
        and delivered.provider_message_id == "99001"
        and delivered.status is OutboundMessageStatus.DELIVERED
        and delivered.final_status is SmsFinalStatus.DELIVERED
        and delivered.voidfix_device_id == "1386"
        and delivered.voidfix_sim_slot == 1
        and delivered.accepted_at == 100.0
        and delivered.provider_sent_date
        and delivered.provider_delivered_date
        and delivered.last_polled_at == 108.0
        and inflight is not None
        and inflight.provider_message_id == "99002"
        and inflight.final_status is SmsFinalStatus.SENT
    )
    report["checks"]["restore_test"] = restored.exists()
    report["checks"]["data_integrity"] = integrity
    report["checks"]["terminal_after_restore"] = (
        delivered is not None and delivered.final_status is SmsFinalStatus.DELIVERED
    )

    # Idempotency on restored DB
    outbox2 = SmsOutbox(store=SqliteSmsOutboxStore(restored))
    idem_ok = False
    try:
        outbox2.reserve(slot_id=1, to_number="+15551234567", idempotency_key="bk-delivered")
    except DuplicateIdempotencyError:
        idem_ok = True
    report["checks"]["idempotency_after_restore"] = idem_ok

    # Delivery recovery without send
    poller = _FakePoller()
    service = SmsDispatchService(
        gateway=_NoSendGateway(),
        isolation=SlotIsolationPolicy({1}),
        device_map={1: "1386"},
        outbox=outbox2,
        delivery_poller=poller,
        voidfix_sim_slots={1: 1},
        live_send_authorized=True,
        dry_run=False,
        recipient_allowlist=("+15551234567",),
    )
    resumed = service.resume_inflight_delivery_tracking()
    after = outbox2.get("bk-inflight")
    report["checks"]["delivery_recovery_after_restore"] = (
        "bk-inflight" in resumed
        and poller.calls == 1
        and after is not None
        and after.final_status is SmsFinalStatus.DELIVERED
    )

    live_hash_after = _sha256(live)
    live_size_after = live.stat().st_size
    report["live_hash_after"] = live_hash_after
    report["checks"]["live_database_protected"] = (
        live_hash_before == live_hash_after and live_size_before == live_size_after
    )

    # Auto-create + schema on fresh path
    fresh = ROOT / "logs" / "backups" / f"sms_outbox-autocreate-{stamp}.sqlite"
    if fresh.exists():
        fresh.unlink()
    SqliteSmsOutboxStore(fresh)
    row = sqlite3_row_version(fresh)
    report["checks"]["auto_create_and_schema"] = fresh.exists() and row == SCHEMA_VERSION

    out = ROOT / "_tmp_outbox_backup_recovery_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("wrote", out)
    return 0 if all(report["checks"].values()) else 1


def sqlite3_row_version(path: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row[0])
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
