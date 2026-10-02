"""In-memory Supabase Auth + tenant stand-ins for tests. No SQLite users."""
from __future__ import annotations

import time
import uuid

from infrastructure.auth_models import AuthUser, SlotOwnership
from infrastructure.supabase_gateway import SupabaseAuthError, SupabaseSession


class MemoryGoTrue:
    project_url = "https://example.supabase.co"

    def __init__(self) -> None:
        self._by_email: dict[str, dict] = {}
        self._by_token: dict[str, AuthUser] = {}
        self._revoked: set[str] = set()

    def signup(self, email: str, password: str) -> SupabaseSession | SupabaseAuthError:
        if email in self._by_email:
            return SupabaseAuthError("invalid_request")
        user = AuthUser(
            user_id=str(uuid.uuid4()),
            email=email,
            is_active=True,
            email_verified=False,
            role="user",
            created_at=time.time(),
            updated_at=time.time(),
        )
        token = f"sb-{user.user_id}"
        self._by_email[email] = {"password": password, "user": user, "token": token}
        self._by_token[token] = user
        return SupabaseSession(user=user, access_token=token, expires_in=3600)

    def login(self, email: str, password: str) -> SupabaseSession | SupabaseAuthError:
        row = self._by_email.get(email)
        if row is None or row["password"] != password or not row["user"].is_active:
            return SupabaseAuthError("invalid_credentials")
        token = row["token"]
        if token in self._revoked:
            token = f"sb-{row['user'].user_id}-{len(self._revoked)}"
            row["token"] = token
            self._by_token[token] = row["user"]
        return SupabaseSession(user=row["user"], access_token=token, expires_in=3600)

    def get_user(self, access_token: str | None) -> AuthUser | None:
        if not access_token or access_token in self._revoked:
            return None
        return self._by_token.get(access_token)

    def logout(self, access_token: str | None) -> bool:
        if access_token:
            self._revoked.add(access_token)
        return True

    def recover(self, email: str) -> bool:
        del email
        return True

    def reset_password(self, token: str, password: str) -> bool:
        del token, password
        return False

    def verify_email(self, token: str) -> bool:
        return bool(token)

    def resend_verification(self, email: str) -> bool:
        del email
        return True


def seed_owned_slot(
    tenant: MemoryTenant,
    *,
    bay: int,
    rental_id: str,
    user_id: str,
    qr_code_url: str = "https://example.test/private/qr",
    carrier_name: str = "T-Mobile",
    imei2: str = "353456789012345",
) -> None:
    tenant.ensure_profile(user_id, "user@example.com")
    tenant.slots[int(bay)] = {
        "id": rental_id,
        "user_id": user_id,
        "rental_id": rental_id,
        "motherboard_slot_num": int(bay),
        "qr_code_url": qr_code_url,
        "carrier_name": carrier_name,
        "imei2": imei2,
    }


class MemoryTenant:
    privileged = True

    def __init__(self) -> None:
        self.profiles: dict[str, str] = {}
        self.slots: dict[int, dict] = {}
        self.esims: list[dict] = []
        self.messages: dict[str, dict] = {}
        self.unclaim_fails = False
        self.unclaim_raises = False

    def close(self) -> None:
        return None

    def ensure_profile(self, user_id: str, email: str) -> None:
        self.profiles[user_id] = email

    def profile_exists(self, user_id: str) -> bool:
        return user_id in self.profiles

    def owner_of_slot(self, farm_slot_id: int) -> str | None:
        row = self.slots.get(int(farm_slot_id))
        if not row:
            return None
        owner = str(row.get("user_id") or "").strip()
        return owner or None

    def list_owned_slots(self, user_id: str) -> list[SlotOwnership]:
        now = time.time()
        owned = []
        for bay, row in sorted(self.slots.items()):
            if row.get("user_id") == user_id:
                owned.append(
                    SlotOwnership(
                        farm_slot_id=bay,
                        user_id=user_id,
                        rental_id=row.get("rental_id"),
                        created_at=now,
                        updated_at=now,
                    )
                )
        return owned

    def claim_slot(self, farm_slot_id: int, user_id: str, rental_id: str | None, *, now: float | None = None) -> bool:
        del now
        bay = int(farm_slot_id)
        existing = self.slots.get(bay)
        if existing and existing.get("user_id") and existing["user_id"] != user_id:
            return False
        previous = dict(existing or {})
        self.slots[bay] = {
            **previous,
            "id": previous.get("id") or rental_id or str(uuid.uuid4()),
            "user_id": user_id,
            "rental_id": rental_id,
            "motherboard_slot_num": previous.get("motherboard_slot_num") or bay,
        }
        return True

    def unclaim_slot(self, farm_slot_id: int, rental_id: str | None, *, now: float | None = None) -> bool:
        del now
        if self.unclaim_raises:
            raise OSError("lovable_unavailable")
        if self.unclaim_fails:
            return False
        bay = int(farm_slot_id)
        existing = self.slots.get(bay)
        if not existing or not str(existing.get("user_id") or "").strip():
            return True
        stored = str(existing.get("rental_id") or existing.get("id") or "").strip()
        want = str(rental_id or "").strip()
        if stored and want and stored != want:
            return False
        self.slots[bay] = {**existing, "user_id": None, "rental_id": None}
        return True

    def get_slot_row(self, farm_slot_id: int) -> dict | None:
        return self.slots.get(int(farm_slot_id))

    def farm_slot_for_slot_id(self, slot_id: str) -> int | None:
        row = self.get_slot_by_id(slot_id)
        if row is None:
            return None
        return int(row.get("motherboard_slot_num") or row.get("bay") or 0) or None

    def get_slot_by_id(self, slot_id: str) -> dict | None:
        text = str(slot_id or "").strip()
        if not text:
            return None
        for bay, row in self.slots.items():
            if str(row.get("id") or "") == text:
                out = dict(row)
                out.setdefault("motherboard_slot_num", bay)
                return out
        return None

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
        record_id = str(uuid.uuid4())
        for existing in self.esims:
            if job_id and existing.get("job_id") == job_id:
                return str(existing.get("id") or "")
        self.esims.append(
            {
                "id": record_id,
                "user_id": user_id,
                "farm_slot_id": farm_slot_id,
                "qr_code_url": storage_key,
                "rental_id": rental_id,
                "carrier": carrier,
                "job_id": job_id,
                "now": now,
            }
        )
        return record_id

    def get_message(self, message_id: str) -> dict | None:
        return self.messages.get(str(message_id))

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
        slot_id = str((self.slots.get(int(farm_slot_id)) or {}).get("id") or "")
        self.messages[message_id] = {
            "message_id": message_id,
            "slot_id": slot_id,
            "direction": direction,
            "phone_number": phone_number,
            "message_body": message_body,
            "status": status,
            "job_id": job_id,
        }

    def update_message_status(self, message_id: str, status: str) -> None:
        return None

    def list_messages(self, farm_slot_id: int, *, limit: int = 50, direction: str | None = None):
        slot_id = str((self.slots.get(int(farm_slot_id)) or {}).get("id") or "")
        rows = [row for row in self.messages.values() if row.get("slot_id") == slot_id]
        if direction in {"inbound", "outbound"}:
            rows = [row for row in rows if row.get("direction") == direction]
        return rows[: max(1, min(limit, 200))]
