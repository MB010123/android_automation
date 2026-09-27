"""Supabase GoTrue client for optional VPS auth proxy and token validation.

Production tenant reads/writes do **not** use this module's PostgREST helpers.
Those helpers remain isolated: they require an explicit service-role key and
never fall back to the anon key. The VPS production path does not pass that
key.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from infrastructure.auth_models import AuthUser

logger = logging.getLogger("vps_backend.supabase")

DEFAULT_TIMEOUT = 10.0


@dataclass
class SupabaseSession:
    user: AuthUser
    access_token: str
    expires_in: int = 3600
    refresh_token: str | None = None


@dataclass
class SupabaseAuthError:
    code: str  # invalid_request | invalid_credentials | unavailable


class SupabaseGateway:
    def __init__(
        self,
        url: str,
        anon_key: str,
        *,
        service_role_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        base = (url or "").strip().rstrip("/")
        if not base:
            raise ValueError("supabase_url_required")
        anon = (anon_key or "").strip()
        if not anon:
            raise ValueError("supabase_anon_key_required")
        self._url = base
        self._anon = anon
        self._service = (service_role_key or "").strip() or None
        self._timeout = timeout_seconds
        self._http = session or requests.Session()

    @property
    def project_url(self) -> str:
        return self._url

    @property
    def has_service_role(self) -> bool:
        return bool(self._service)

    def close(self) -> None:
        self._http.close()

    def signup(self, email: str, password: str) -> SupabaseSession | SupabaseAuthError:
        payload = self._gotrue(
            "POST",
            "/auth/v1/signup",
            json_body={"email": email, "password": password},
        )
        if isinstance(payload, SupabaseAuthError):
            if payload.code == "invalid_credentials":
                return SupabaseAuthError("invalid_request")
            return payload
        user = _user_from_gotrue(payload.get("user") or payload)
        if user is None:
            return SupabaseAuthError("invalid_request")
        token = str(payload.get("access_token") or "").strip()
        if not token:
            return SupabaseSession(user=user, access_token="", expires_in=0)
        return SupabaseSession(
            user=user,
            access_token=token,
            expires_in=_expires_in(payload),
            refresh_token=_optional_str(payload.get("refresh_token")),
        )

    def login(self, email: str, password: str) -> SupabaseSession | SupabaseAuthError:
        payload = self._gotrue(
            "POST",
            "/auth/v1/token?grant_type=password",
            json_body={"email": email, "password": password},
        )
        if isinstance(payload, SupabaseAuthError):
            return payload
        user = _user_from_gotrue(payload.get("user") or payload)
        token = str(payload.get("access_token") or "").strip()
        if user is None or not token:
            return SupabaseAuthError("invalid_credentials")
        return SupabaseSession(
            user=user,
            access_token=token,
            expires_in=_expires_in(payload),
            refresh_token=_optional_str(payload.get("refresh_token")),
        )

    def get_user(self, access_token: str | None) -> AuthUser | None:
        if not access_token:
            return None
        payload = self._gotrue("GET", "/auth/v1/user", user_token=access_token)
        if isinstance(payload, SupabaseAuthError):
            return None
        return _user_from_gotrue(payload)

    def logout(self, access_token: str | None) -> bool:
        if not access_token:
            return False
        payload = self._gotrue("POST", "/auth/v1/logout", json_body={}, user_token=access_token)
        return not isinstance(payload, SupabaseAuthError) or payload.code != "unavailable"

    def recover(self, email: str) -> bool:
        payload = self._gotrue("POST", "/auth/v1/recover", json_body={"email": email})
        return not isinstance(payload, SupabaseAuthError) or payload.code != "unavailable"

    def reset_password(self, token: str, password: str) -> bool:
        verified = self._gotrue(
            "POST",
            "/auth/v1/verify",
            json_body={"type": "recovery", "token": token},
        )
        if isinstance(verified, SupabaseAuthError):
            return False
        access = str(verified.get("access_token") or "").strip()
        if not access:
            return False
        updated = self._gotrue(
            "PUT",
            "/auth/v1/user",
            json_body={"password": password},
            user_token=access,
        )
        return not isinstance(updated, SupabaseAuthError)

    def verify_email(self, token: str) -> bool:
        payload = self._gotrue(
            "POST",
            "/auth/v1/verify",
            json_body={"type": "signup", "token": token},
        )
        return not isinstance(payload, SupabaseAuthError)

    def resend_verification(self, email: str) -> bool:
        payload = self._gotrue(
            "POST",
            "/auth/v1/resend",
            json_body={"type": "signup", "email": email},
        )
        return not isinstance(payload, SupabaseAuthError) or payload.code != "unavailable"

    def rest_select(
        self,
        table: str,
        *,
        query: str,
        user_token: str | None = None,
    ) -> list[dict[str, Any]] | None:
        payload = self._rest("GET", table, query=query, user_token=user_token)
        if payload is None:
            return None
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        return None

    def rest_insert(
        self,
        table: str,
        body: dict[str, Any],
        *,
        prefer: str = "return=representation",
    ) -> list[dict[str, Any]] | None:
        payload = self._rest("POST", table, json_body=body, prefer=prefer)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            return [payload]
        return None

    def rest_upsert(
        self,
        table: str,
        body: dict[str, Any],
        *,
        on_conflict: str,
    ) -> bool:
        payload = self._rest(
            "POST",
            table,
            json_body=body,
            prefer="return=minimal,resolution=merge-duplicates",
            query=f"on_conflict={quote(on_conflict, safe='')}",
        )
        return payload is not None

    def rest_patch(
        self,
        table: str,
        *,
        query: str,
        body: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        payload = self._rest("PATCH", table, query=query, json_body=body, prefer="return=representation")
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            return [payload]
        return None

    def _gotrue(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        user_token: str | None = None,
    ) -> dict[str, Any] | SupabaseAuthError:
        bearer = user_token or self._anon
        try:
            response = self._http.request(
                method,
                f"{self._url}{path}",
                json=json_body,
                headers={
                    "apikey": self._anon,
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except (requests.RequestException, OSError, ConnectionError):
            logger.warning("supabase_auth_unavailable method=%s path=%s", method, path.split("?", 1)[0])
            return SupabaseAuthError("unavailable")
        if 200 <= response.status_code < 300:
            if not response.content:
                return {}
            try:
                data = response.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
        if response.status_code in (400, 401):
            return SupabaseAuthError("invalid_credentials")
        if response.status_code in (409, 422):
            return SupabaseAuthError("invalid_request")
        logger.warning(
            "supabase_auth_http status=%s path=%s",
            response.status_code,
            path.split("?", 1)[0],
        )
        if response.status_code >= 500:
            return SupabaseAuthError("unavailable")
        return SupabaseAuthError("invalid_request")

    def _rest(
        self,
        method: str,
        table: str,
        *,
        query: str = "",
        json_body: dict[str, Any] | None = None,
        prefer: str = "return=representation",
        user_token: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any | None:
        if not _safe_table(table):
            return None
        if not self._service:
            logger.error("supabase_service_role_required table=%s method=%s", table, method)
            return None
        key = self._service
        bearer = user_token or key
        url = f"{self._url}/rest/v1/{table}"
        if query:
            url = f"{url}?{query}"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": prefer,
        }
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self._http.request(
                method,
                url,
                json=json_body,
                headers=headers,
                timeout=self._timeout,
            )
        except (requests.RequestException, OSError, ConnectionError):
            logger.warning("supabase_rest_unavailable table=%s method=%s", table, method)
            return None
        if 200 <= response.status_code < 300:
            if not response.content:
                return []
            try:
                return response.json()
            except ValueError:
                return []
        logger.warning(
            "supabase_rest_http table=%s method=%s status=%s",
            table,
            method,
            response.status_code,
        )
        return None


def gotrue_from_env(*, session: requests.Session | None = None) -> SupabaseGateway | None:
    """Auth-only gateway. Never reads SUPABASE_SERVICE_ROLE_KEY."""
    url = (os.getenv("SUPABASE_URL") or "").strip()
    anon = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
    if not url or not anon:
        return None
    return SupabaseGateway(
        url,
        anon,
        service_role_key=None,
        timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT))),
        session=session,
    )


def _safe_table(table: str) -> bool:
    return bool(table) and table.replace("_", "").isalnum()


def _expires_in(payload: dict[str, Any]) -> int:
    raw = payload.get("expires_in")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 3600
    return value if value > 0 else 3600


def _optional_str(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _user_from_gotrue(data: object) -> AuthUser | None:
    if not isinstance(data, dict):
        return None
    user_id = str(data.get("id") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    if not user_id or not email:
        return None
    banned = data.get("banned_until")
    return AuthUser(
        user_id=user_id,
        email=email,
        password_hash="",
        is_active=not banned,
        email_verified=bool(data.get("email_confirmed_at") or data.get("confirmed_at")),
        role="user",
        created_at=_parse_ts(data.get("created_at")),
        updated_at=_parse_ts(data.get("updated_at") or data.get("created_at")),
        last_login_at=_parse_ts(data.get("last_sign_in_at")) or None,
        failed_login_attempts=0,
        locked_until=None,
    )


def _parse_ts(value: object) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
