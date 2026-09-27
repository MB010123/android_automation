"""Unused production adapter: direct privileged PostgREST tenant access.

Production VPS tenant I/O uses `LovableTenantStore`. This class is kept for
isolated tests of the fail-closed PostgREST path. It never falls back to the
anon key.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

from infrastructure.auth_models import SlotOwnership
from infrastructure.supabase_gateway import SupabaseGateway

logger = logging.getLogger("vps_backend.supabase_tenant")

SLOT_SAFE_FIELDS = (
    "id",
    "user_id",
    "status",
    "hardware_box_id",
    "motherboard_slot_num",
    "carrier_name",
    "phone_number",
    "imei2",
    "last_heartbeat",
    "band_lock_setting",
    "proxy_address",
    "gateway_provider",
    "created_at",
)


class SupabaseTenantStore:
    def __init__(self, gateway: SupabaseGateway, *, default_box: str = "POD_01") -> None:
        self._gw = gateway
        self._default_box = default_box

    @property
    def privileged(self) -> bool:
        return self._gw.has_service_role

    def close(self) -> None:
        self._gw.close()

    def ensure_profile(self, user_id: str, email: str) -> None:
        ok = self._gw.rest_upsert(
            "profiles",
            {"id": user_id, "email": email},
            on_conflict="id",
        )
        if not ok:
            logger.warning("supabase_profile_upsert_failed")

    def owner_of_slot(self, farm_slot_id: int) -> str | None:
        row = self.get_slot_row(farm_slot_id)
        if row is None:
            return None
        owner = str(row.get("user_id") or "").strip()
        return owner or None

    def list_owned_slots(self, user_id: str) -> list[SlotOwnership]:
        rows = self._gw.rest_select(
            "slots",
            query=f"user_id=eq.{quote(user_id, safe='')}&select=motherboard_slot_num,user_id,created_at&order=motherboard_slot_num.asc",
        )
        owned: list[SlotOwnership] = []
        if not rows:
            return owned
        now = time.time()
        for row in rows:
            bay = _as_int(row.get("motherboard_slot_num"))
            if bay is None:
                continue
            created = _as_float(row.get("created_at"), now)
            owned.append(
                SlotOwnership(
                    farm_slot_id=bay,
                    user_id=str(row.get("user_id") or user_id),
                    rental_id=None,
                    created_at=created,
                    updated_at=created,
                )
            )
        return owned

    def claim_slot(self, farm_slot_id: int, user_id: str, rental_id: str | None, *, now: float | None = None) -> bool:
        del now
        existing = self.get_slot_row(farm_slot_id)
        if existing is not None:
            current = str(existing.get("user_id") or "").strip()
            if current and current != user_id:
                return False
            patched = self._gw.rest_patch(
                "slots",
                query=f"motherboard_slot_num=eq.{int(farm_slot_id)}",
                body={"user_id": user_id, "status": "provisioning"},
            )
            return patched is not None
        inserted = self._gw.rest_insert(
            "slots",
            {
                "user_id": user_id,
                "status": "provisioning",
                "hardware_box_id": self._default_box,
                "motherboard_slot_num": int(farm_slot_id),
            },
        )
        return inserted is not None

    def get_slot_row(self, farm_slot_id: int) -> dict[str, Any] | None:
        rows = self._gw.rest_select(
            "slots",
            query=f"motherboard_slot_num=eq.{int(farm_slot_id)}&select={','.join(SLOT_SAFE_FIELDS)}&limit=1",
        )
        if not rows:
            return None
        return rows[0]

    def profile_exists(self, user_id: str) -> bool:
        rows = self._gw.rest_select(
            "profiles",
            query=f"id=eq.{quote(user_id, safe='')}&select=id&limit=1",
        )
        return bool(rows)

    def farm_slot_for_slot_id(self, slot_id: str) -> int | None:
        text = str(slot_id or "").strip()
        if not text:
            return None
        rows = self._gw.rest_select(
            "slots",
            query=f"id=eq.{quote(text, safe='')}&select=motherboard_slot_num&limit=1",
        )
        if not rows:
            return None
        return _as_int(rows[0].get("motherboard_slot_num"))

    def resolve_slot_id(self, farm_slot_id: int) -> str | None:
        row = self.get_slot_row(farm_slot_id)
        if row is None:
            return None
        slot_id = str(row.get("id") or "").strip()
        return slot_id or None

    def record_esim_upload(
        self,
        *,
        user_id: str,
        farm_slot_id: int,
        storage_key: str,
        rental_id: str | None,
        carrier: str | None,
        job_id: str | None,
        now: float | None = None,
    ) -> str:
        del user_id, rental_id, carrier, job_id, now
        slot_id = self.resolve_slot_id(farm_slot_id)
        if not slot_id:
            logger.warning("supabase_esim_skipped_no_slot bay=%s", farm_slot_id)
            return ""
        rows = self._gw.rest_insert(
            "esim_uploads",
            {"slot_id": slot_id, "qr_code_url": storage_key},
        )
        if not rows:
            return ""
        return str(rows[0].get("id") or "")

    def record_message(
        self,
        *,
        farm_slot_id: int,
        message_id: str,
        direction: str,
        phone_number: str | None,
        message_body: str | None,
        status: str,
        job_id: str | None = None,
    ) -> None:
        slot_id = self.resolve_slot_id(farm_slot_id)
        if not slot_id:
            logger.warning("supabase_message_skipped_no_slot bay=%s", farm_slot_id)
            return
        body: dict[str, Any] = {
            "slot_id": slot_id,
            "message_id": message_id,
            "direction": direction,
            "phone_number": phone_number,
            "message_body": message_body,
            "status": status,
        }
        if job_id:
            body["job_id"] = job_id
        if self._gw.rest_insert("messages", body) is None:
            logger.warning("supabase_message_insert_failed bay=%s", farm_slot_id)

    def update_message_status(self, message_id: str, status: str) -> None:
        patched = self._gw.rest_patch(
            "messages",
            query=f"message_id=eq.{quote(message_id, safe='')}",
            body={"status": status},
        )
        if patched is None:
            logger.warning("supabase_message_status_failed")

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        text = str(message_id or "").strip()
        if not text:
            return None
        rows = self._gw.rest_select(
            "messages",
            query=f"message_id=eq.{quote(text, safe='')}&select=*&limit=1",
        )
        if not rows:
            return None
        return rows[0]

    def list_messages(
        self,
        farm_slot_id: int,
        *,
        limit: int = 50,
        direction: str | None = None,
    ) -> list[dict[str, Any]] | None:
        slot_id = self.resolve_slot_id(farm_slot_id)
        if not slot_id:
            return []
        query = f"slot_id=eq.{quote(slot_id, safe='')}&select=*&order=created_at.desc&limit={max(1, min(limit, 200))}"
        if direction in {"inbound", "outbound"}:
            query = f"direction=eq.{direction}&{query}"
        return self._gw.rest_select("messages", query=query)


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object, default: float) -> float:
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default
