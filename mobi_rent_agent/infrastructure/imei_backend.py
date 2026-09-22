"""Push Slot IMEI 2 to the Lovable/Supabase slot record.

Rental lookup is:

    assigned bay (motherboard_slot_num) -> public.slots.imei2

The hardware queue UUID is slots.id. Heartbeat still sends the physical
bay as integer slot_id. This client never sends IMEI 1, never sends an
ADB serial, and never logs a raw IMEI.

The public hardware API is GET/POST
``/api/public/hardware/queue``. public.slots.imei2 must be selected and
returned on GET for verification. This module does not enable eSIM
provisioning, SOCKS5, VoidFix, or auto-recovery.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

from infrastructure.imei import is_valid_imei, redact_imei

logger = logging.getLogger("mobi_rent_agent.imei_backend")

IMEI2_DDL = "alter table public.slots add column if not exists imei2 text;"
_MISSING_COLUMN_RE = re.compile(r"imei2.*does not exist|Could not find the .*imei2", re.IGNORECASE)


class Imei2BackendError(RuntimeError):
    """The backend IMEI 2 write or lookup failed."""


@dataclass(frozen=True)
class BackendSlotRecord:
    """One hardware-queue / slots row, keyed by physical bay."""

    record_id: str
    motherboard_slot_num: int | None
    hardware_box_id: str | None
    status: str | None
    imei2: str | None

    def redacted_imei2(self) -> str:
        return redact_imei(self.imei2)


@dataclass(frozen=True)
class Imei2SyncResult:
    bay: int
    record_id: str | None
    posted: bool
    persisted: bool
    imei2_redacted: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_id": self.bay,
            "backend_record_id": self.record_id,
            "lookup": "motherboard_slot_num -> imei2",
            "posted": self.posted,
            "persisted": self.persisted,
            "imei2_redacted": self.imei2_redacted,
            "imei1_sent": False,
            "error": self.error,
        }


class HttpImei2Backend:
    """Reads and writes digital IMEI on the existing hardware queue."""

    def __init__(
        self,
        endpoint: str,
        hardware_agent_token: str,
        timeout_seconds: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._token = hardware_agent_token
        self._timeout = timeout_seconds
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {hardware_agent_token}",
                "X-Hardware-Agent-Token": hardware_agent_token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_queue(self) -> list[BackendSlotRecord]:
        try:
            response = self._session.get(self._endpoint, timeout=self._timeout)
        except requests.exceptions.RequestException as exc:
            raise Imei2BackendError(f"hardware queue get failed: {exc}") from exc
        if not (200 <= response.status_code < 300):
            raise Imei2BackendError(
                f"hardware queue get status={response.status_code} body={_safe_body(response)}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise Imei2BackendError("hardware queue get returned non-JSON") from exc
        return [_parse_queue_row(row, index) for index, row in enumerate(_as_rows(body))]

    def slot_for_bay(self, bay: int) -> BackendSlotRecord | None:
        matches = [row for row in self.fetch_queue() if row.motherboard_slot_num == bay]
        if not matches:
            return None
        if len(matches) > 1:
            raise Imei2BackendError(f"multiple backend rows have motherboard_slot_num={bay}")
        return matches[0]

    def imei2_for_assigned_slot(self, assigned_slot_id: int) -> str | None:
        """Rental -> assigned bay -> backend imei2. Does not read the phone."""
        record = self.slot_for_bay(assigned_slot_id)
        return record.imei2 if record else None

    def register_imei2(
        self,
        bay: int,
        imei2: str,
        allow_slots_2_20: bool = False,
        adb_serial: str | None = None,
    ) -> Imei2SyncResult:
        if bay != 1 and not allow_slots_2_20:
            raise Imei2BackendError("refusing to modify slots 2-20")
        if not is_valid_imei(imei2):
            raise Imei2BackendError("imei2 must be a valid 15-digit IMEI")
        if adb_serial and imei2 == adb_serial:
            raise Imei2BackendError("imei2 must not equal the ADB serial")

        existing = self.slot_for_bay(bay)
        if existing is None:
            raise Imei2BackendError(
                f"no backend slots row with motherboard_slot_num={bay}"
            )
        if not existing.status:
            raise Imei2BackendError(
                f"backend slot {bay} has no status; refusing to guess one"
            )
        payload = {
            "hardware_agent_token": self._token,
            "slot_id": bay,
            "status": existing.status,
            "imei2": imei2,
        }
        logger.info(
            "Posting imei2 for motherboard_slot_num=%s record=%s imei2=%s",
            bay,
            existing.record_id if existing else "<missing>",
            redact_imei(imei2),
        )
        try:
            response = self._session.post(self._endpoint, json=payload, timeout=self._timeout)
        except requests.exceptions.RequestException as exc:
            raise Imei2BackendError(f"hardware queue imei2 post failed: {exc}") from exc

        if not (200 <= response.status_code < 300):
            body = _safe_body(response)
            if _MISSING_COLUMN_RE.search(body) or "42703" in body or "PGRST204" in body:
                raise Imei2BackendError(
                    "public.slots has no imei2 column. Add it with: " + IMEI2_DDL
                )
            raise Imei2BackendError(
                f"hardware queue imei2 post status={response.status_code} body={body}"
            )

        stored = self.imei2_for_assigned_slot(bay)
        persisted = stored == imei2
        if not persisted:
            after = self.slot_for_bay(bay)
            raise Imei2BackendError(
                "Slot "
                f"{bay} backend record did not return imei2 "
                f"(record={after.record_id if after else '<missing>'}, "
                f"has_imei2_field={after is not None and after.imei2 is not None}, "
                f"imei2={redact_imei(stored)}). "
                "GET /api/public/hardware/queue must include imei2 for "
                "motherboard_slot_num, and POST must persist it on that row."
            )
        logger.info(
            "Persisted imei2 motherboard_slot_num=%s imei2=%s",
            bay,
            redact_imei(imei2),
        )
        after = self.slot_for_bay(bay)
        return Imei2SyncResult(
            bay=bay,
            record_id=after.record_id if after else None,
            posted=True,
            persisted=True,
            imei2_redacted=redact_imei(imei2),
        )

    def close(self) -> None:
        self._session.close()


def _as_rows(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict):
        for key in ("slots", "data", "queue", "records"):
            value = body.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        if "slot_id" in body or "motherboard_slot_num" in body:
            return [body]
    raise Imei2BackendError("hardware queue response must be a list of slot objects")


def _parse_queue_row(row: dict[str, Any], index: int) -> BackendSlotRecord:
    record_id = row.get("slot_id")
    if record_id is None:
        record_id = row.get("id")
    if record_id is None:
        raise Imei2BackendError(f"hardware queue row {index} is missing slot_id")
    bay = row.get("motherboard_slot_num")
    if bay is not None:
        try:
            bay = int(bay)
        except (TypeError, ValueError) as exc:
            raise Imei2BackendError(
                f"hardware queue row {index} has invalid motherboard_slot_num"
            ) from exc
    imei2 = row.get("imei2")
    if imei2 is not None:
        imei2 = str(imei2)
    return BackendSlotRecord(
        record_id=str(record_id),
        motherboard_slot_num=bay,
        hardware_box_id=str(row["hardware_box_id"]) if row.get("hardware_box_id") is not None else None,
        status=str(row["status"]) if row.get("status") is not None else None,
        imei2=imei2,
    )


def _safe_body(response: requests.Response, limit: int = 400) -> str:
    try:
        text = response.text[:limit]
    except Exception:  # pragma: no cover - defensive
        return "<unreadable response body>"
    return re.sub(r"\d{15}", lambda match: redact_imei(match.group(0)), text)
