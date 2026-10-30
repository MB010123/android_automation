"""VPS auth front door: proxy Supabase Auth; never issue a second user JWT."""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from application.admin_identity import resolve_admin_role
from application.vps_api_contract import error_body, iso_ts
from infrastructure.auth_models import AuthUser
from infrastructure.supabase_gateway import SupabaseAuthError, SupabaseGateway, SupabaseSession
from infrastructure.supabase_jwt import verify_supabase_access_token

logger = logging.getLogger("vps_backend.auth")

TOKEN_CACHE_TTL_SECONDS = 300.0
TOKEN_CACHE_MAX_ENTRIES = 4096

EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
MAX_EMAIL_LEN = 254
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 128
MAX_JSON_FIELD = 512
MAX_ESIM_URL_FIELD = 4096


@dataclass
class AuthContext:
    kind: str  # user | farm_service
    user: AuthUser | None = None
    session_id: str | None = None
    access_token: str | None = None


@dataclass
class AuthResult:
    http_status: int
    body: dict[str, Any]
    access_token: str | None = None


@dataclass
class _CachedToken:
    user: AuthUser
    claims: dict[str, Any] | None
    verified_at: float
    token_expires_at: float


class AuthService:
    def __init__(
        self,
        *,
        supabase: SupabaseGateway,
        tenant: Any = None,
        jwt_secret: str = "",
        access_ttl_seconds: int = 3600,
        admin_user_ids: frozenset[str] | set[str] | tuple[str, ...] = (),
        clock: Callable[[], float] | None = None,
    ) -> None:
        if supabase is None:
            raise ValueError("supabase_required")
        self._supabase = supabase
        self._tenant = tenant
        self._jwt_secret = (jwt_secret or "").strip()
        self._ttl = int(access_ttl_seconds)
        self._admin_user_ids = frozenset(str(uid).strip() for uid in admin_user_ids if str(uid).strip())
        self._clock = clock or time.time
        self._token_cache: dict[str, _CachedToken] = {}
        self._token_lock = threading.Lock()

    @property
    def access_ttl_seconds(self) -> int:
        return self._ttl

    def signup(
        self,
        payload: dict[str, Any],
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuthResult:
        del ip, user_agent
        email, password, err = _parse_credentials(payload)
        if err is not None:
            return err
        outcome = self._supabase.signup(email, password)
        if isinstance(outcome, SupabaseAuthError):
            if outcome.code == "unavailable":
                return AuthResult(503, error_body("auth_unavailable"))
            return AuthResult(400, error_body("invalid_request", message="Unable to create account"))
        self._ensure_profile(outcome.user)
        logger.info("auth_signup user_id=%s", outcome.user.user_id)
        return self._session_result(outcome)

    def login(
        self,
        payload: dict[str, Any],
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuthResult:
        del ip, user_agent
        email, password, err = _parse_credentials(payload)
        if err is not None:
            return AuthResult(401, error_body("invalid_credentials"))
        outcome = self._supabase.login(email, password)
        if isinstance(outcome, SupabaseAuthError):
            if outcome.code == "unavailable":
                return AuthResult(503, error_body("auth_unavailable"))
            return AuthResult(401, error_body("invalid_credentials"))
        self._ensure_profile(outcome.user)
        logger.info("auth_login user_id=%s", outcome.user.user_id)
        return self._session_result(outcome)

    def validate_user_token(self, token: str | None) -> AuthContext | None:
        if not token:
            return None
        now = float(self._clock())
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._cached_token(digest, now)
        if cached is not None and (now - cached.verified_at) < TOKEN_CACHE_TTL_SECONDS:
            user = self._with_admin_role(cached.user, cached.claims)
            return AuthContext(kind="user", user=user, session_id=user.user_id, access_token=token)
        user, claims = self._user_from_local_jwt(token)
        transient = False
        if user is None:
            user, transient = self._user_from_gotrue(token)
            claims = None
            if user is None and transient:
                user, transient = self._user_from_gotrue(token)
        if user is None:
            if transient and cached is not None:
                user = cached.user
                claims = cached.claims
            else:
                return None
        if not user.is_active:
            return None
        user = self._with_admin_role(user, claims)
        self._remember_token(digest, user, claims, now)
        return AuthContext(kind="user", user=user, session_id=user.user_id, access_token=token)

    def logout(self, ctx: AuthContext) -> AuthResult:
        if ctx.kind != "user" or not ctx.session_id:
            return AuthResult(401, error_body("unauthorized"))
        if ctx.access_token:
            digest = hashlib.sha256(ctx.access_token.encode("utf-8")).hexdigest()
            with self._token_lock:
                self._token_cache.pop(digest, None)
        self._supabase.logout(ctx.access_token)
        return AuthResult(200, {"ok": True})

    def me(self, ctx: AuthContext) -> AuthResult:
        if ctx.kind != "user" or ctx.user is None:
            return AuthResult(401, error_body("unauthorized"))
        return AuthResult(200, {"ok": True, "user": _public_user(ctx.user, full=True)})

    def session_body(self, ctx: AuthContext) -> AuthResult:
        if ctx.kind == "farm_service":
            return AuthResult(
                200,
                {
                    "ok": True,
                    "authenticated": True,
                    "audience": "farm_service",
                    "token_type": "FARM_SERVICE_TOKEN",
                    "message": "Machine-to-machine farm service credential. Not a user session.",
                },
            )
        if ctx.kind != "user" or ctx.user is None:
            return AuthResult(401, error_body("unauthorized"))
        return AuthResult(
            200,
            {
                "ok": True,
                "authenticated": True,
                "audience": "user",
                "token_type": "USER_ACCESS_TOKEN",
                "session_id": ctx.session_id,
                "user": _public_user(ctx.user, full=True),
            },
        )

    def forgot_password(self, payload: dict[str, Any]) -> AuthResult:
        email = _normalize_email(payload.get("email"))
        generic = AuthResult(200, {"ok": True})
        if email is None:
            return generic
        self._supabase.recover(email)
        return generic

    def reset_password(self, payload: dict[str, Any]) -> AuthResult:
        token = str(payload.get("token") or "").strip()
        password = payload.get("password")
        if not token or not isinstance(password, str):
            return AuthResult(400, error_body("invalid_request"))
        if _password_error(password, email="") is not None:
            return AuthResult(400, error_body("weak_password"))
        if not self._supabase.reset_password(token, password):
            return AuthResult(400, error_body("invalid_request"))
        return AuthResult(200, {"ok": True})

    def resend_verification(self, ctx: AuthContext) -> AuthResult:
        if ctx.kind != "user" or ctx.user is None:
            return AuthResult(401, error_body("unauthorized"))
        self._supabase.resend_verification(ctx.user.email)
        return AuthResult(200, {"ok": True})

    def verify_email(self, payload: dict[str, Any]) -> AuthResult:
        token = str(payload.get("token") or "").strip()
        if not token:
            return AuthResult(400, error_body("invalid_request"))
        if not self._supabase.verify_email(token):
            return AuthResult(400, error_body("invalid_request"))
        return AuthResult(200, {"ok": True})

    def _cached_token(self, digest: str, now: float) -> _CachedToken | None:
        with self._token_lock:
            rec = self._token_cache.get(digest)
            if rec is None:
                return None
            if now >= rec.token_expires_at:
                self._token_cache.pop(digest, None)
                return None
            return rec

    def _remember_token(
        self,
        digest: str,
        user: AuthUser,
        claims: dict[str, Any] | None,
        now: float,
    ) -> None:
        token_exp = now + TOKEN_CACHE_TTL_SECONDS
        if isinstance(claims, dict):
            try:
                exp = float(claims.get("exp"))
            except (TypeError, ValueError):
                exp = 0.0
            if exp > now:
                token_exp = max(token_exp, exp)
        with self._token_lock:
            if len(self._token_cache) >= TOKEN_CACHE_MAX_ENTRIES:
                expired = [key for key, rec in self._token_cache.items() if rec.token_expires_at <= now]
                for key in expired:
                    self._token_cache.pop(key, None)
                if len(self._token_cache) >= TOKEN_CACHE_MAX_ENTRIES:
                    oldest = min(self._token_cache, key=lambda k: self._token_cache[k].verified_at)
                    self._token_cache.pop(oldest, None)
            self._token_cache[digest] = _CachedToken(
                user=replace(user),
                claims=dict(claims) if isinstance(claims, dict) else None,
                verified_at=now,
                token_expires_at=token_exp,
            )

    def _user_from_gotrue(self, token: str) -> tuple[AuthUser | None, bool]:
        try_get = getattr(self._supabase, "try_get_user", None)
        if callable(try_get):
            try:
                user, err = try_get(token)
            except (OSError, ConnectionError, TimeoutError):
                return None, True
            return user, err == "unavailable"
        try:
            user = self._supabase.get_user(token)
        except (OSError, ConnectionError, TimeoutError):
            return None, True
        return user, False

    def _user_from_local_jwt(self, token: str) -> tuple[AuthUser | None, dict[str, Any] | None]:
        if not self._jwt_secret:
            return None, None
        claims = verify_supabase_access_token(
            token,
            self._jwt_secret,
            issuer=self._supabase.project_url,
        )
        if claims is None:
            return None, None
        email = str(claims.get("email") or "").strip().lower()
        user_id = str(claims.get("sub") or "").strip()
        if not user_id:
            return None, None
        return (
            AuthUser(
                user_id=user_id,
                email=email,
                is_active=True,
                email_verified=bool(claims.get("email_verified") or claims.get("email_confirmed_at")),
                role="user",
            ),
            claims,
        )

    def _with_admin_role(self, user: AuthUser, claims: dict[str, Any] | None) -> AuthUser:
        role = resolve_admin_role(
            user_id=user.user_id,
            allowlist=self._admin_user_ids,
            claims=claims,
        )
        # Never keep a role supplied by get_user / client metadata.
        return replace(user, role=role)

    def _ensure_profile(self, user: AuthUser) -> None:
        if self._tenant is None:
            return
        self._tenant.ensure_profile(user.user_id, user.email)

    def _session_result(self, session: SupabaseSession) -> AuthResult:
        if not session.access_token:
            return AuthResult(
                200,
                {"ok": True, "user": _public_user(session.user, full=False), "session": None},
            )
        body: dict[str, Any] = {
            "ok": True,
            "user": _public_user(session.user, full=False),
            "session": {
                "access_token": session.access_token,
                "token_type": "Bearer",
                "expires_in": session.expires_in or self._ttl,
            },
        }
        if session.refresh_token:
            body["session"]["refresh_token"] = session.refresh_token
        return AuthResult(200, body, access_token=session.access_token)


def _public_user(user: AuthUser, *, full: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": user.user_id,
        "email": user.email,
        "email_verified": user.email_verified,
    }
    if full:
        body["created_at"] = iso_ts(user.created_at)
        body["last_login_at"] = iso_ts(user.last_login_at)
        body["role"] = user.role
    return body


def _parse_credentials(payload: dict[str, Any]) -> tuple[str, str, AuthResult | None]:
    if not isinstance(payload, dict):
        return "", "", AuthResult(400, error_body("invalid_json"))
    email = _normalize_email(payload.get("email"))
    password = payload.get("password")
    if email is None:
        return "", "", AuthResult(400, error_body("invalid_email"))
    if not isinstance(password, str):
        return "", "", AuthResult(400, error_body("weak_password"))
    pw_err = _password_error(password, email=email)
    if pw_err is not None:
        return "", "", AuthResult(400, error_body(pw_err))
    return email, password, None


def _normalize_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or len(email) > MAX_EMAIL_LEN:
        return None
    if not EMAIL_RE.match(email):
        return None
    return email


def _password_error(password: str, *, email: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN or len(password) > MAX_PASSWORD_LEN:
        return "weak_password"
    if password.strip() != password or not password.strip():
        return "weak_password"
    if password.lower() == email or (email and email.split("@")[0] in password.lower() and len(email.split("@")[0]) >= 4):
        return "weak_password"
    if len(set(password)) == 1:
        return "weak_password"
    return None


def validate_esim_storage_key(value: str, *, allowed_url_prefixes: tuple[str, ...] = ()) -> str | None:
    """Accept an internal object key or an allowlisted private-storage URL. Never fetch it."""
    key = value.strip()
    if not key:
        return None
    if "://" in key:
        if len(key) > MAX_ESIM_URL_FIELD:
            return None
        if any(key.startswith(prefix) for prefix in allowed_url_prefixes if prefix):
            return key
        return None
    if len(key) > MAX_JSON_FIELD:
        return None
    if re.fullmatch(r"[A-Za-z0-9/_.\-]+", key):
        return key
    return None
