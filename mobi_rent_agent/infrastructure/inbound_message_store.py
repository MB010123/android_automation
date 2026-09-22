"""Durable storage for inbound SMS received on the VPS webhook (server-side state)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1


class InboundMessageStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "schema_version" not in tables:
            self._conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            self._conn.execute(
                """
                CREATE TABLE inbound_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at REAL NOT NULL,
                    device_id TEXT,
                    slot_id INTEGER,
                    from_number TEXT NOT NULL,
                    body TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            self._conn.commit()
            return
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None or int(row["version"]) != SCHEMA_VERSION:
            raise RuntimeError(f"inbound store schema unsupported (expected {SCHEMA_VERSION})")

    def insert(
        self,
        *,
        device_id: str | None,
        slot_id: int | None,
        from_number: str,
        body: str,
        payload: object,
    ) -> int:
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False)
        cur = self._conn.execute(
            """
            INSERT INTO inbound_messages
                (received_at, device_id, slot_id, from_number, body, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, device_id, slot_id, from_number, body, payload_json),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM inbound_messages").fetchone()
        return int(row["c"]) if row else 0

    def close(self) -> None:
        self._conn.close()
