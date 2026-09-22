"""Analyze slot2-slot1 test without sending SMS."""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

NGROK = "http://127.0.0.1:4040/api/requests/http?limit=200"


def main() -> None:
    con = sqlite3.connect(ROOT / "logs" / "sms_outbox.sqlite")
    rows = con.execute(
        "SELECT idempotency_key, slot_id, status, final_status, provider_message_id, error "
        "FROM outbound_messages WHERE idempotency_key LIKE 'slot2-slot1-inbound%' "
        "ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    print("OUTBOX", json.dumps(rows, indent=2))

    with urllib.request.urlopen(NGROK, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    posts = []
    for req in data.get("requests") or []:
        r = req.get("request") or {}
        uri = r.get("uri") or ""
        if (r.get("method") or "").upper() == "POST" and "voidfix/inbound" in uri:
            posts.append(
                {
                    "start": req.get("start"),
                    "status": (req.get("response") or {}).get("status"),
                    "uri": uri,
                    "body_len": len((r.get("raw") or r.get("body") or "") or ""),
                }
            )
    print("NGROK_POSTS", json.dumps(posts, indent=2))

    cap = ROOT / "logs" / "voidfix_inbound_webhook_captures"
    for p in sorted(cap.glob("inbound_*.json")):
        print("CAPTURE", p.name, p.stat().st_mtime)


if __name__ == "__main__":
    main()
