"""Authoritative tenant adapter over the Lovable server API.

Production VPS code uses this store instead of privileged PostgREST.
`SupabaseTenantStore` remains only as an unused/isolated adapter for tests.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from infrastructure.auth_models import SlotOwnership
from infrastructure.lovable_server_client import LovableApiResult, LovableServerClient, lovable_client_from_env

logger = logging.getLogger("vps_backend.lovable_tenant")


class UnconfiguredTenantStore:
    """Fail-closed stand-in when the Lovable machine credential is missing."""

    privileged = False

    def close(self) -> None:
        return None

    def ensure_profile(self, user_id: str, email: str) -> None:
        del user_id, email

    def profile_exists(self, user_id: str) -> bool:
        del user_id
        return False

    def owner_of_slot(self, farm_slot_id: int) -> str | None:
        del farm_slot_id
        return None

    def list_owned_slots(self, user_id: str) -> list[SlotOwnership]:
        del user_id
        return []

    def claim_slot(self, farm_slot_id: int, user_id: str, rental_id: str | None, *, now: float | None = None) -> bool:
        del farm_slot_id, user_id, rental_id, now
        return False

    def get_slot_row(self, farm_slot_id: int) -> dict[str, Any] | None:
        del farm_slot_id
        return None

    def farm_slot_for_slot_id(self, slot_id: str) -> int | None:
        del slot_id
        return None

    def get_slot_by_id(self, slot_id: str) -> dict[str, Any] | None:
        del slot_id
        return None

    def record_esim_upload(self, **kwargs: Any) -> str:
        del kwargs
        return ""

    def record_message(self, **kwargs: Any) -> None:
        del kwargs

    def update_message_status(self, message_id: str, status: str) -> None:
        del message_id, status

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        del message_id
        return None

    def list_messages(self, farm_slot_id: int, *, limit: int = 50, direction: str | None = None) -> list[dict[str, Any]]:
        del farm_slot_id, limit, direction
        return []


class LovableTenantStore:
    def __init__(self, client: LovableServerClient, *, default_box: str = "POD_01") -> None:
        self._client = client
        self._default_box = default_box

    @property
    def privileged(self) -> bool:
        return self._client.configured

    def close(self) -> None:
        self._client.close()

    def ensure_profile(self, user_id: str, email: str) -> None:
        result = self._client.request(
            "PUT",
            f"/profiles/{user_id}",
            json_body={"email": email},
            idempotency_key=f"profile-{user_id}",
        )
        if result.error:
            logger.warning("lovable_profile_upsert_failed")

    def profile_exists(self, user_id: str) -> bool:
        result = self._require(self._client.request("GET", f"/profiles/{user_id}"))
        return result.error is None

    def owner_of_slot(self, farm_slot_id: int) -> str | None:
        row = self.get_slot_row(farm_slot_id)
        if row is None:
            return None
        owner = str(row.get("user_id") or "").strip()
        return owner or None

    def list_owned_slots(self, user_id: str) -> list[SlotOwnership]:
        result = self._require(
            self._client.request("GET", "/slots", query={"user_id": user_id}),
        )
        rows = _as_list(result.body, key="slots")
        owned: list[SlotOwnership] = []
        now = time.time()
        for row in rows:
            bay = _as_int(row.get("motherboard_slot_num") or row.get("bay"))
            if bay is None:
                continue
            created = _as_float(row.get("created_at"), now)
            owned.append(
                SlotOwnership(
                    farm_slot_id=bay,
                    user_id=str(row.get("user_id") or user_id),
                    rental_id=row.get("rental_id"),
                    created_at=created,
                    updated_at=created,
                )
            )
        return owned

    def claim_slot(self, farm_slot_id: int, user_id: str, rental_id: str | None, *, now: float | None = None) -> bool:
        del now
        result = self._require(
            self._client.request(
                "POST",
                f"/slots/by-bay/{int(farm_slot_id)}/claim",
                json_body={"user_id": user_id, "rental_id": rental_id},
                idempotency_key=f"claim-{farm_slot_id}-{user_id}-{rental_id or 'none'}",
            )
        )
        if result.error in {"conflict", "invalid", "unauthorized", "unavailable"}:
            return False
        return result.error is None

    def get_slot_row(self, farm_slot_id: int) -> dict[str, Any] | None:
        result = self._require(
            self._client.request("GET", f"/slots/by-bay/{int(farm_slot_id)}"),
        )
        if result.error == "not_found":
            return None
        row = _as_object(result.body, key="slot")
        return row or None

    def farm_slot_for_slot_id(self, slot_id: str) -> int | None:
        row = self.get_slot_by_id(slot_id)
        if row is None:
            return None
        return _as_int(row.get("motherboard_slot_num") or row.get("bay"))

    def get_slot_by_id(self, slot_id: str) -> dict[str, Any] | None:
        text = str(slot_id or "").strip()
        if not text:
            return None
        result = self._require(self._client.request("GET", f"/slots/{text}"))
        if result.error == "not_found":
            return None
        if result.error:
            raise OSError("lovable_unavailable")
        row = _as_object(result.body, key="slot")
        return row or None

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
        del now
        idem = f"esim-{job_id}" if job_id else f"esim-{farm_slot_id}-{storage_key}"
        result = self._client.request(
            "POST",
            "/esim-uploads",
            json_body={
                "user_id": user_id,
                "farm_slot_id": int(farm_slot_id),
                "qr_code_url": storage_key,
                "rental_id": rental_id,
                "carrier": carrier,
                "job_id": job_id,
            },
            idempotency_key=idem,
        )
        if result.error:
            logger.warning("lovable_esim_upload_failed bay=%s", farm_slot_id)
            return ""
        row = _as_object(result.body, key="upload") or (result.body if isinstance(result.body, dict) else {})
        return str(row.get("id") or "")

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
        result = self._client.request(
            "POST",
            "/messages",
            json_body={
                "farm_slot_id": int(farm_slot_id),
                "message_id": message_id,
                "direction": direction,
                "phone_number": phone_number,
                "message_body": message_body,
                "status": status,
                "job_id": job_id,
            },
            idempotency_key=f"message-{message_id}",
        )
        if result.error:
            logger.warning("lovable_message_insert_failed bay=%s", farm_slot_id)

    def update_message_status(self, message_id: str, status: str) -> None:
        result = self._client.request(
            "PATCH",
            f"/messages/{message_id}",
            json_body={"status": status},
        )
        if result.error:
            logger.warning("lovable_message_status_failed")

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        text = str(message_id or "").strip()
        if not text:
            return None
        result = self._require(self._client.request("GET", f"/messages/{text}"))
        if result.error:
            return None
        return _as_object(result.body, key="message") or (result.body if isinstance(result.body, dict) else None)

    def list_messages(
        self,
        farm_slot_id: int,
        *,
        limit: int = 50,
        direction: str | None = None,
    ) -> list[dict[str, Any]] | None:
        query = {"limit": str(max(1, min(limit, 200)))}
        if direction in {"inbound", "outbound"}:
            query["direction"] = direction
        result = self._require(
            self._client.request("GET", f"/slots/by-bay/{int(farm_slot_id)}/messages", query=query),
        )
        if result.error == "not_found":
            return []
        return _as_list(result.body, key="messages")

    def _require(self, result: LovableApiResult) -> LovableApiResult:
        if result.error in {"unavailable", "unauthorized"}:
            raise OSError("lovable_unavailable")
        return result


def tenant_store_from_env(*, session: Any = None, default_box: str = "POD_01") -> Any:
    client = lovable_client_from_env(session=session)
    if client is None:
        logger.error("lovable_machine_credential_required")
        return UnconfiguredTenantStore()
    return LovableTenantStore(client, default_box=default_box)


def _as_object(payload: Any, *, key: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        inner = payload.get(key)
        if isinstance(inner, dict):
            return inner
        if any(k in payload for k in ("id", "user_id", "motherboard_slot_num", "imei2", "message_id")):
            return payload
    return {}


def _as_list(payload: Any, *, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


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
