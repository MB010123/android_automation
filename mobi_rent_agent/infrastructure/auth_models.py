"""Identity objects derived only from a validated Supabase access token."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AuthUser:
    user_id: str
    email: str
    password_hash: str = ""
    is_active: bool = True
    email_verified: bool = False
    role: str = "user"
    created_at: float = 0.0
    updated_at: float = 0.0
    last_login_at: float | None = None
    failed_login_attempts: int = 0
    locked_until: float | None = None


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str


@dataclass
class SlotOwnership:
    farm_slot_id: int
    user_id: str
    rental_id: str | None
    created_at: float
    updated_at: float
