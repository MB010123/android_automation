"""Prepare full-box SMS test matrix (read-only). Does not send SMS or mutate .env."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLAN = ROOT / "_tmp_fullbox_prep_test_plan.md"
OUT_JSON = ROOT / "_tmp_fullbox_test_config.json"
OUT_MD = ROOT / "_tmp_fullbox_test_config.md"
OUTBOX = ROOT / "logs" / "sms_outbox.sqlite"

PROTOTYPE_VF = "1385"
PROTOTYPE_SERIAL = "3C071JEHN14705"

# Recommended simultaneous outbound: one hub handset receives all 20 texts.
HUB_RECIPIENT = "+19522287088"
OUTBOUND_TEMPLATE = "Mobi-Rent fullbox OUT slot={slot:02d} run={run_id}"
INBOUND_REPLY_TEMPLATE = "Mobi-Rent fullbox IN slot={slot:02d} run={run_id}"

# Authoritative identity rows from _tmp_fullbox_prep_test_plan.md (2026-09-21 prep).
MATRIX = [
    (1, "18171FDF6005WG", "1386", "+19522287088"),
    (2, "19141FDF6OO8T9", "1389", "+16514722709"),
    (3, "19161FDF6004AD", "1393", "+17633287165"),
    (4, "19161FDF6005B9", "1394", "+17633468225"),
    (5, "19161FDF6OO411", "1395", "+16126499401"),
    (6, "19281FDF6OO1T4", "1402", "+17633390175"),
    (7, "1A181FDF6006KN", "1403", "+16126499481"),
    (8, "1B131FDF60090W", "1404", "+17633573736"),
    (9, "1B301FDF6004HF", "1405", "+16126499603"),
    (10, "1C021FDF600GKL", "1406", "+17633406158"),
    (11, "1C071FDF6004MS", "1407", "+16126499651"),
    (12, "1C071FDF6006YS", "1408", "+17634382664"),
    (13, "1C111FDF600CNB", "1409", "+16126499683"),
    (14, "1C141FDF600GXF", "1410", "+17633935186"),
    (15, "21051FDF600EM9", "1411", "+16126499573"),
    (16, "23101FDF60058N", "1412", "+17634384448"),
    (17, "25061FDF6006KF", "1413", "+17633887556"),
    (18, "25261FDF60017D", "1414", "+17634388552"),
    (19, "1C101FDF6009EZ", "1415", "+16126499751"),
    (20, "1A271FDF600BX2", "1417", "+17633935255"),
]

RING_RECIPIENTS = {
    slot: MATRIX[(idx + 1) % len(MATRIX)][3] for idx, (slot, *_rest) in enumerate(MATRIX)
}


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def outbox_row_count() -> int:
    if not OUTBOX.exists():
        return 0
    con = sqlite3.connect(OUTBOX)
    try:
        return con.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    if not PLAN.is_file():
        print(f"FAIL: missing authoritative plan {PLAN}")
        return 2

    sys_path = str(ROOT)
    import sys

    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)

    from infrastructure.config import _parse_csv
    from infrastructure.redact import normalize_msisdn
    from infrastructure.voidfix_devices import load_voidfix_device_map

    def _read_env(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if not path.is_file():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip().strip('"').strip("'")
        return out

    rows_before = outbox_row_count()
    env = _read_env(ROOT / ".env")
    devices_path = env.get("VOIDFIX_DEVICES_PATH") or str(ROOT / "voidfix_devices.json")
    device_map = load_voidfix_device_map(devices_path)
    allowlist = {
        normalize_msisdn(x)
        for x in _parse_csv(env.get("VOIDFIX_RECIPIENT_ALLOWLIST"))
        if str(x).strip()
    }
    inbound_endpoint = env.get("VOIDFIX_INBOUND_ENDPOINT") or None

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hub_ok = normalize_msisdn(HUB_RECIPIENT) in allowlist

    slots: list[dict] = []
    for slot, serial, vf, sim2 in MATRIX:
        if vf == PROTOTYPE_VF or serial == PROTOTYPE_SERIAL:
            raise RuntimeError(f"prototype device must stay excluded: slot={slot}")
        mapped_vf = str(device_map.get(slot, ""))
        if mapped_vf != vf:
            raise RuntimeError(f"slot {slot} voidfix mismatch: config={mapped_vf} plan={vf}")
        slots.append(
            {
                "slot": slot,
                "adb_serial": serial,
                "voidfix_device_id": vf,
                "sim2_msisdn": sim2,
                "outbound_test_recipient": HUB_RECIPIENT,
                "outbound_message": OUTBOUND_TEMPLATE.format(slot=slot, run_id=run_id),
                "expected_inbound_reply_target": sim2,
                "inbound_reply_message": INBOUND_REPLY_TEMPLATE.format(slot=slot, run_id=run_id),
                "notes": (
                    "slot 1 SIM2 equals hub MSISDN; outbound is farm→hub self-number text"
                    if slot == 1
                    else ""
                ),
                "allowlist_covers_outbound": normalize_msisdn(HUB_RECIPIENT) in allowlist,
            }
        )

    hub_recipients = sorted({normalize_msisdn(HUB_RECIPIENT)})
    ring_recipients = sorted({normalize_msisdn(n) for n in RING_RECIPIENTS.values()})
    missing_for_ring = sorted(set(ring_recipients) - allowlist)
    missing_for_hub: list[str] = [] if hub_ok else hub_recipients

    payload = {
        "prepared_at_utc": ts(),
        "sms_sent_during_preparation": False,
        "outbox_rows_before": rows_before,
        "authoritative_plan": str(PLAN.name),
        "prototype_excluded": {"voidfix_device_id": PROTOTYPE_VF, "adb_serial": PROTOTYPE_SERIAL},
        "outbound_strategy": {
            "name": "central_hub",
            "description": "All 20 slots send outbound to one allowlisted hub; correlate by unique body token per slot.",
            "distinct_recipients_required": 1,
            "recipients_e164": hub_recipients,
        },
        "alternate_outbound_strategy": {
            "name": "ring_to_next_slot_sim2",
            "description": "Slot N texts slot N+1 SIM2 MSISDN (slot 20 → slot 1). Requires every farm SIM2 on allowlist.",
            "distinct_recipients_required": 20,
            "recipients_e164": ring_recipients,
            "allowlist_missing_for_ring": missing_for_ring,
        },
        "current_recipient_allowlist": sorted(allowlist),
        "required_allowlist_changes": {
            "for_central_hub_test": {
                "add": missing_for_hub,
                "remove": [],
                "note": "No change if hub +19522287088 remains the sole recipient.",
            },
            "for_ring_test": {
                "add": missing_for_ring,
                "remove": [],
                "note": "Not recommended unless explicitly authorized; expands blast radius to all farm MSISDNs.",
            },
        },
        "inbound_validation_plan": {
            "reply_initiator": "Manual or scripted SMS from hub handset +19522287088 to each expected_inbound_reply_target",
            "production_poll_inbound_endpoint": inbound_endpoint,
            "outbound_delivery_verification": {
                "method": "SmsDispatchService send + durable outbox + VoidFixDeliveryPoller on read-messages.php",
                "match_keys": [
                    "slot_id",
                    "voidfix_device_id",
                    "idempotency_key",
                    "provider_message_id",
                    "message body token slot=NN",
                    "deliveredDate on read-messages row",
                ],
            },
            "inbound_reply_verification": {
                "primary": "SmsDispatchService.ingest_inbound(webhook_payload) when VoidFix dashboard webhook hits agent HTTPS URL",
                "match_keys": [
                    "device_id → slot via voidfix_devices.json reverse map",
                    "from_number hub +19522287088",
                    "message body token IN slot=NN run=…",
                    "to implicit via device_id (SIM2 on that phone)",
                ],
                "fallback_without_inbound_endpoint": [
                    "VoidFix read-messages.php poll filtered by deviceID == voidfix_device_id; locate newest row with sender hub and IN token",
                    "Optional read-only ADB SMS inbox spot-check per adb_serial if API schema lacks inbound direction",
                ],
            },
        },
        "eventual_test_entrypoint": {
            "factory_path": "application.sms_factory.build_sms_dispatch_service",
            "send_api": "SmsDispatchService.send_for_slot(slot_id, to_number, message, idempotency_key=...)",
            "reference_tool": "tools/slot1_sms_delivery_regression.py",
            "planned_fullbox_tool": "tools/fullbox_sms_simultaneous_test.py",
            "example_command_after_authorization": (
                f"cd {ROOT.name} && python tools/fullbox_sms_simultaneous_test.py "
                f"--config _tmp_fullbox_test_config.json --execute"
            ),
            "constraints": [
                "No raw VoidFix send scripts",
                "No daemon restart required",
                "VOIDFIX_LIVE_SEND_AUTHORIZED must remain true",
                "Recipient must be on VOIDFIX_RECIPIENT_ALLOWLIST",
            ],
        },
        "run_id": run_id,
        "slots": slots,
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    md_lines = [
        "# Full-box SMS test configuration (preparation only)",
        "",
        f"Prepared: `{payload['prepared_at_utc']}` · Run id: `{run_id}`",
        "",
        "**No SMS was sent during this step.**",
        "",
        "## Outbound strategy (recommended)",
        "",
        f"All 20 slots → **{HUB_RECIPIENT}** (1 distinct recipient).",
        "",
        "| Slot | ADB serial | VoidFix | SIM2 MSISDN | Outbound recipient | Expected inbound reply target |",
        "|------|------------|---------|-------------|-------------------|------------------------------|",
    ]
    for s in slots:
        md_lines.append(
            f"| {s['slot']} | `{s['adb_serial']}` | {s['voidfix_device_id']} | {s['sim2_msisdn']} | "
            f"{s['outbound_test_recipient']} | {s['expected_inbound_reply_target']} |"
        )
    md_lines.extend(
        [
            "",
            "## Allowlist",
            "",
            f"Current: `{', '.join(payload['current_recipient_allowlist']) or '(empty)'}`",
            "",
            "**Central hub test:** no additions required.",
            "",
            f"**Ring test (optional):** would need **{len(missing_for_ring)}** additions: "
            + (", ".join(missing_for_ring) if missing_for_ring else "(none)"),
            "",
            "## Inbound verification (summary)",
            "",
            "1. Hub replies once per slot to each **Expected inbound reply target** with the planned `IN slot=NN` token.",
            "2. Match `device_id` from webhook/`ingest_inbound` to slot; confirm `from_number` is the hub.",
            "3. Outbound leg: outbox + `deliveredDate` per `provider_message_id`.",
            "",
            "## Eventual command path",
            "",
            f"`python tools/fullbox_sms_simultaneous_test.py --config _tmp_fullbox_test_config.json --execute`",
            "",
            "(Tool sends only after explicit `--execute`; uses `build_sms_dispatch_service`.)",
        ]
    )
    OUT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    rows_after = outbox_row_count()
    payload["outbox_rows_after"] = rows_after
    if rows_after != rows_before:
        print("WARN: outbox row count changed during read-only prep")
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(json.dumps({"ok": True, "wrote": [str(OUT_JSON.name), str(OUT_MD.name)], "sms_sent": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
