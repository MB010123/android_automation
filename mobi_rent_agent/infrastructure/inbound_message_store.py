"""Durable storage for inbound SMS received on the VPS webhook (server-side state)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 2


class InboundMessageStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
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
                    payload_json TEXT NOT NULL,
                    message_uuid TEXT,
                    provider_message_id TEXT,
                    slot_public_id TEXT,
                    direction TEXT DEFAULT 'in',
                    to_number TEXT
                )
                """
            )
            self._conn.commit()
            return
        self._migrate_to_v2()

    def _migrate_to_v2(self) -> None:
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(inbound_messages)").fetchall()
        }
        if "message_uuid" not in cols:
            self._conn.execute("ALTER TABLE inbound_messages ADD COLUMN message_uuid TEXT")
            self._conn.execute("ALTER TABLE inbound_messages ADD COLUMN provider_message_id TEXT")
            self._conn.execute("ALTER TABLE inbound_messages ADD COLUMN slot_public_id TEXT")
            self._conn.execute("ALTER TABLE inbound_messages ADD COLUMN direction TEXT DEFAULT 'in'")
            self._conn.execute("ALTER TABLE inbound_messages ADD COLUMN to_number TEXT")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_provider_message_id "
                "ON inbound_messages (provider_message_id) "
                "WHERE provider_message_id IS NOT NULL"
            )
            self._conn.commit()
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        if row is not None:
            self._conn.execute(
                "UPDATE schema_version SET version = ? WHERE rowid = 1",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()

    def insert(
        self,
        *,
        device_id: str | None,
        slot_id: int | None,
        from_number: str,
        body: str,
        payload: object,
        provider_message_id: str | None = None,
        slot_public_id: str | None = None,
        to_number: str | None = None,
    ) -> tuple[int, bool]:
        """Insert row. Returns (row_id, created). Skips duplicate provider_message_id."""
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            if provider_message_id:
                existing = self._conn.execute(
                    "SELECT id FROM inbound_messages WHERE provider_message_id = ?",
                    (provider_message_id,),
                ).fetchone()
                if existing:
                    return int(existing["id"]), False
            message_uuid = str(uuid.uuid4())
            cur = self._conn.execute(
                """
                INSERT INTO inbound_messages (
                    received_at, device_id, slot_id, from_number, body, payload_json,
                    message_uuid, provider_message_id, slot_public_id, direction, to_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'in', ?)
                """,
                (
                    now,
                    device_id,
                    slot_id,
                    from_number,
                    body,
                    payload_json,
                    message_uuid,
                    provider_message_id,
                    slot_public_id,
                    to_number,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid), True

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM inbound_messages").fetchone()
        return int(row["c"]) if row else 0

    def close(self) -> None:
        self._conn.close()
