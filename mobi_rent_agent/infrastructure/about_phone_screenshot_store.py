"""Per-slot About Phone screenshot map.

The first verified capture for a bay is saved. Later requests serve that
photo instead of driving Settings again. A missing or serial-mismatched
entry means take a new screenshot.
"""
from __future__ import annotations

import base64
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from domain.farm import MAX_FARM_SLOT_ID

SCHEMA_VERSION = 1
MIN_PNG_BYTES = 256
_PNG_MAGIC = b"\x89PNG"
_PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(frozen=True)
class AboutPhoneScreenshot:
    farm_slot_id: int
    serial: str | None
    image_base64: str
    captured_at: float


def decode_about_phone_png(image_base64: str | None) -> bytes | None:
    text = str(image_base64 or "").strip()
    if not text:
        return None
    if "," in text and text.lower().startswith("data:image/"):
        text = text.split(",", 1)[1]
    try:
        raw = base64.b64decode(text, validate=False)
    except (ValueError, TypeError):
        return None
    if not raw.startswith(_PNG_MAGIC) or raw == _PLACEHOLDER_PNG or len(raw) < MIN_PNG_BYTES:
        return None
    return raw


def normalize_about_phone_image(image_base64: str | None) -> str | None:
    raw = decode_about_phone_png(image_base64)
    if raw is None:
        return None
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


class AboutPhoneScreenshotStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    @property
    def db_path(self) -> Path:
        return self._path

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS about_phone_schema_version (
                version INTEGER NOT NULL
            )
            """
        )
        row = self._conn.execute("SELECT version FROM about_phone_schema_version").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO about_phone_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS about_phone_screenshots (
                farm_slot_id INTEGER PRIMARY KEY,
                serial TEXT,
                image_base64 TEXT NOT NULL,
                captured_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, farm_slot_id: int, *, serial: str | None = None) -> AboutPhoneScreenshot | None:
        try:
            slot = int(farm_slot_id)
        except (TypeError, ValueError):
            return None
        if not 1 <= slot <= MAX_FARM_SLOT_ID:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT farm_slot_id, serial, image_base64, captured_at "
                "FROM about_phone_screenshots WHERE farm_slot_id = ?",
                (slot,),
            ).fetchone()
        if row is None:
            return None
        image = normalize_about_phone_image(row[2])
        if image is None:
            return None
        stored_serial = str(row[1] or "").strip() or None
        want = str(serial or "").strip() or None
        if want and stored_serial and want != stored_serial:
            return None
        return AboutPhoneScreenshot(
            farm_slot_id=int(row[0]),
            serial=stored_serial,
            image_base64=image,
            captured_at=float(row[3]),
        )

    def put(
        self,
        farm_slot_id: int,
        image_base64: str,
        *,
        serial: str | None = None,
        now: float | None = None,
    ) -> AboutPhoneScreenshot | None:
        try:
            slot = int(farm_slot_id)
        except (TypeError, ValueError):
            return None
        if not 1 <= slot <= MAX_FARM_SLOT_ID:
            return None
        image = normalize_about_phone_image(image_base64)
        if image is None:
            return None
        captured = float(now if now is not None else time.time())
        stored_serial = str(serial or "").strip() or None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO about_phone_screenshots (
                    farm_slot_id, serial, image_base64, captured_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(farm_slot_id) DO UPDATE SET
                    serial = excluded.serial,
                    image_base64 = excluded.image_base64,
                    captured_at = excluded.captured_at
                """,
                (slot, stored_serial, image, captured),
            )
            self._conn.commit()
        return AboutPhoneScreenshot(
            farm_slot_id=slot,
            serial=stored_serial,
            image_base64=image,
            captured_at=captured,
        )
