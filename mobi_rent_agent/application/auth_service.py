"""VPS user authentication: signup, login, sessions, reset/verify tokens."""
from __future__ import annotations

import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from application.vps_api_contract import error_body, iso_ts
from infrastructure.password_hasher import hash_password, verify_password
from infrastructure.user_jwt import sha256_hex, sign_user_jwt, verify_user_jwt
from infrastructure.vps_auth_store import AuthUser, VpsAuthStore

logger = logging.getLogger("vps_backend.auth")

EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
MAX_EMAIL_LEN = 254
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 128
MAX_JSON_FIELD = 512
MIN_JWT_SECRET_LENGTH = 32


def jwt_secret_usable(secret: str) -> bool:
    """True when the HMAC secret meets the production minimum (32 UTF-8 bytes)."""
    return bool(secret) and len(secret.encode("utf-8")) >= MIN_JWT_SECRET_LENGTH


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


class AuthService:
    def __init__(
        self,
        store: VpsAuthStore,
        *,
        jwt_secret: str,
        access_ttl_seconds: int = 3600,
        lock_after: int = 5,
        lock_seconds: float = 900.0,
        reset_ttl_seconds: float = 3600.0,
        verify_ttl_seconds: float = 86400.0,
        clock: Callable[[], float] | None = None,
        token_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        if not jwt_secret_usable(jwt_secret):
            raise ValueError("jwt_secret_too_short")
        self._store = store
        self._secret = jwt_secret
        self._ttl = int(access_ttl_seconds)
        self._lock_after = lock_after
        self._lock_seconds = lock_seconds
        self._reset_ttl = reset_ttl_seconds
        self._verify_ttl = verify_ttl_seconds
        self._clock = clock or time.time
        self._token_sink = token_sink

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
        email, password, err = _parse_credentials(payload)
        if err is not None:
            return err
        user = self._store.create_user(email=email, password_hash=hash_password(password), now=self._clock())
        if user is None:
            return AuthResult(400, error_body("invalid_request", message="Unable to create account"))
        verify_raw = secrets.token_urlsafe(32)
        self._store.create_one_time_token(
            "email_verify_tokens",
            user_id=user.user_id,
            token_hash=sha256_hex(verify_raw),
            ttl_seconds=self._verify_ttl,
            now=self._clock(),
        )
        if self._token_sink is not None:
            self._token_sink("verify", verify_raw)
        logger.info("auth_signup user_id=%s", user.user_id)
        return self._issue_session(user, ip=ip, user_agent=user_agent)

    def login(
        self,
        payload: dict[str, Any],
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuthResult:
        email, password, err = _parse_credentials(payload)
        if err is not None:
            return AuthResult(401, error_body("invalid_credentials"))
        user = self._store.get_user_by_email(email)
        dummy = hash_password("timing-balance-unused")
        if user is None:
            verify_password(password, dummy)
            return AuthResult(401, error_body("invalid_credentials"))
        now = self._clock()
        if user.locked_until is not None and user.locked_until > now:
            verify_password(password, dummy)
            return AuthResult(401, error_body("invalid_credentials"))
        if not user.is_active:
            verify_password(password, dummy)
            return AuthResult(401, error_body("invalid_credentials"))
        if not verify_password(password, user.password_hash):
            self._store.record_login_failure(
                user.user_id,
                lock_after=self._lock_after,
                lock_seconds=self._lock_seconds,
                now=now,
            )
            return AuthResult(401, error_body("invalid_credentials"))
        self._store.record_login_success(user.user_id, now=now)
        user = self._store.get_user_by_id(user.user_id) or user
        logger.info("auth_login user_id=%s", user.user_id)
        return self._issue_session(user, ip=ip, user_agent=user_agent)

    def validate_user_token(self, token: str | None) -> AuthContext | None:
        if not token or not self._secret:
            return None
        claims = verify_user_jwt(token, self._secret, now=self._clock())
        if claims is None:
            return None
        session_id = str(claims.get("jti") or "")
        user_id = str(claims.get("sub") or "")
        session = self._store.get_session(session_id)
        if session is None or session.user_id != user_id:
            return None
        now = self._clock()
        if session.revoked_at is not None or session.expires_at <= now:
            return None
        if session.token_hash != sha256_hex(token):
            return None
        user = self._store.get_user_by_id(user_id)
        if user is None or not user.is_active:
            return None
        self._store.touch_session(session_id, now=now)
        return AuthContext(kind="user", user=user, session_id=session_id, access_token=token)

    def logout(self, ctx: AuthContext) -> AuthResult:
        if ctx.kind != "user" or not ctx.session_id:
            return AuthResult(401, error_body("unauthorized"))
        self._store.revoke_session(ctx.session_id, now=self._clock())
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
        user = self._store.get_user_by_email(email)
        if user is None or not user.is_active:
            return generic
        raw = secrets.token_urlsafe(32)
        self._store.create_one_time_token(
            "password_reset_tokens",
            user_id=user.user_id,
            token_hash=sha256_hex(raw),
            ttl_seconds=self._reset_ttl,
            now=self._clock(),
        )
        if self._token_sink is not None:
            self._token_sink("reset", raw)
        logger.info("auth_password_reset_issued user_id=%s", user.user_id)
        return generic

    def reset_password(self, payload: dict[str, Any]) -> AuthResult:
        token = str(payload.get("token") or "").strip()
        password = payload.get("password")
        if not token or not isinstance(password, str):
            return AuthResult(400, error_body("invalid_request"))
        if _password_error(password, email="") is not None:
            return AuthResult(400, error_body("weak_password"))
        user_id = self._store.consume_one_time_token(
            "password_reset_tokens",
            sha256_hex(token),
            now=self._clock(),
        )
        if user_id is None:
            return AuthResult(400, error_body("invalid_request"))
        self._store.set_password_hash(user_id, hash_password(password), now=self._clock())
        self._store.revoke_sessions_for_user(user_id, now=self._clock())
        return AuthResult(200, {"ok": True})

    def resend_verification(self, ctx: AuthContext) -> AuthResult:
        if ctx.kind != "user" or ctx.user is None:
            return AuthResult(401, error_body("unauthorized"))
        raw = secrets.token_urlsafe(32)
        self._store.create_one_time_token(
            "email_verify_tokens",
            user_id=ctx.user.user_id,
            token_hash=sha256_hex(raw),
            ttl_seconds=self._verify_ttl,
            now=self._clock(),
        )
        if self._token_sink is not None:
            self._token_sink("verify", raw)
        return AuthResult(200, {"ok": True})

    def verify_email(self, payload: dict[str, Any]) -> AuthResult:
        token = str(payload.get("token") or "").strip()
        if not token:
            return AuthResult(400, error_body("invalid_request"))
        user_id = self._store.consume_one_time_token(
            "email_verify_tokens",
            sha256_hex(token),
            now=self._clock(),
        )
        if user_id is None:
            return AuthResult(400, error_body("invalid_request"))
        self._store.set_email_verified(user_id, now=self._clock())
        return AuthResult(200, {"ok": True})

    def _issue_session(self, user: AuthUser, *, ip: str | None, user_agent: str | None) -> AuthResult:
        session_id = str(uuid.uuid4())
        now = self._clock()
        token = sign_user_jwt(
            user_id=user.user_id,
            session_id=session_id,
            secret=self._secret,
            ttl_seconds=self._ttl,
            now=now,
        )
        self._store.create_session(
            session_id=session_id,
            user_id=user.user_id,
            token_hash=sha256_hex(token),
            expires_at=now + self._ttl,
            ip=ip,
            user_agent=_clip(user_agent, 256),
            now=now,
        )
        return AuthResult(
            200,
            {
                "ok": True,
                "user": _public_user(user, full=False),
                "session": {
                    "access_token": token,
                    "token_type": "Bearer",
                    "expires_in": self._ttl,
                },
            },
            access_token=token,
        )


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


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


def validate_esim_storage_key(value: str, *, allowed_url_prefixes: tuple[str, ...] = ()) -> str | None:
    """Accept an internal object key or an allowlisted private-storage URL. Never fetch it."""
    key = value.strip()
    if not key or len(key) > MAX_JSON_FIELD:
        return None
    if "://" in key:
        if any(key.startswith(prefix) for prefix in allowed_url_prefixes if prefix):
            return key
        return None
    if re.fullmatch(r"[A-Za-z0-9/_.\-]+", key):
        return key
    return None
