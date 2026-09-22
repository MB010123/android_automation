"""Read-only Slot 19 inbound failure investigation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

RUN = "FULL-INBOUND-20260921T220753Z"
PIDS = {"18": "3957001", "19": "3957003", "20": "3957005"}
DEV = {"18": "1414", "19": "1415", "20": "1417"}


def _api_key() -> str:
    for ln in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if ln.startswith("VOIDFIX_API_KEY="):
            return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> None:
    from infrastructure.voidfix_delivery import VoidFixDeliveryPoller, find_message_by_id
    from infrastructure.voidfix_api import _coerce_message_list

    key = _api_key()
    poller = VoidFixDeliveryPoller(key)
    payload = poller.fetch_messages_payload()
    msgs = _coerce_message_list(payload)

    print("=== OUTBOUND ROWS (send IDs) ===")
    for slot, pid in PIDS.items():
        row = find_message_by_id(payload, pid)
        if not row:
            print(slot, pid, "NOT_IN_READ_MESSAGES")
            continue
        print(
            slot,
            pid,
            {
                "deviceID": row.get("deviceID"),
                "number": row.get("number"),
                "status": row.get("status"),
                "deliveredDate": row.get("deliveredDate"),
                "message_head": str(row.get("message", ""))[:60],
            },
        )

    print("\n=== INBOUND-LIKE (Received + run + device) ===")
    for slot, vf in DEV.items():
        hits = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            if str(m.get("deviceID")) != vf:
                continue
            if RUN not in str(m.get("message", "")):
                continue
            hits.append(
                {
                    "ID": m.get("ID"),
                    "status": m.get("status"),
                    "number": m.get("number"),
                    "sentDate": m.get("sentDate"),
                    "deliveredDate": m.get("deliveredDate"),
                }
            )
        print(f"slot {slot} vf {vf} hits", json.dumps(hits, indent=2))

    print("\n=== ALL Received rows mentioning run on any device ===")
    any_run = [
        {
            "ID": m.get("ID"),
            "deviceID": m.get("deviceID"),
            "status": m.get("status"),
            "number": m.get("number"),
        }
        for m in msgs
        if isinstance(m, dict)
        and RUN in str(m.get("message", ""))
        and str(m.get("status", "")).lower() == "received"
    ]
    print(json.dumps(any_run, indent=2))


if __name__ == "__main__":
    main()
