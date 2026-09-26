"""Lovable Cloud Auth boundary for the VPS.

User signup/login are hosted by Lovable Cloud Auth (Supabase auth.users).
The VPS never accepts, stores, hashes, or logs passwords. These routes exist
so a mistaken call to the farm API returns a truthful integration contract
instead of a fake session.
"""
from __future__ import annotations

from typing import Any

from application.vps_api_contract import error_body

PASSWORD_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pass",
        "secret",
        "new_password",
        "current_password",
        "access_token",
        "refresh_token",
        "id_token",
    }
)

AUTH_NOT_HOSTED = "auth_not_hosted_on_vps"


def auth_refusal_body(action: str) -> dict[str, Any]:
    body = error_body(
        AUTH_NOT_HOSTED,
        message=(
            "User sign-up and login are hosted by Lovable Cloud Auth "
            "(supabase.auth.signUp / signInWithPassword). "
            "The VPS does not issue user JWTs and does not store passwords."
        ),
    )
    body["action"] = action
    body["provider"] = "lovable_cloud_auth"
    body["hosted_on"] = "lovable"
    body["use"] = {
        "signup": "supabase.auth.signUp({ email, password })",
        "login": "supabase.auth.signInWithPassword({ email, password })",
        "profile_trigger": "handle_new_user → public.profiles (id = auth.uid())",
        "session": "browser sb-*-auth-token; gate src/routes/_authenticated/route.tsx",
        "rls": "auth.uid() = user_id on public.slots / public.messages / public.esim_uploads",
        "vps": (
            "After the user is authenticated, Lovable server-side functions call "
            "the VPS with Authorization: Bearer <FARM_SERVICE_TOKEN>. "
            "Never put FARM_SERVICE_TOKEN or user passwords in browser code."
        ),
    }
    return body


def farm_service_session_body() -> dict[str, Any]:
    return {
        "ok": True,
        "authenticated": True,
        "audience": "farm_service",
        "user_auth": "lovable_cloud_auth",
        "message": (
            "This bearer token is FARM_SERVICE_TOKEN (Lovable server → VPS), "
            "not a user session. User identity lives in auth.users / public.profiles."
        ),
    }


def drop_sensitive_fields(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy with password/token keys removed. Never log the original."""
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if str(k).lower() not in PASSWORD_KEYS}
