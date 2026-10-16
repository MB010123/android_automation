"""Server-side administrator identity. Never trust a request-body role claim."""
from __future__ import annotations

from typing import Any, Mapping

from infrastructure.auth_models import AuthUser

_ADMIN = "admin"
_USER = "user"
_ADMIN_APP_METADATA_KEYS = ("mobi_rent_role", "role")


def resolve_admin_role(
    *,
    user_id: str,
    allowlist: frozenset[str] | set[str] | tuple[str, ...] = (),
    claims: Mapping[str, Any] | None = None,
) -> str:
    """Return ``admin`` only from verified JWT subject / app_metadata.

    Sources, in order:
    1. ``user_id`` is in the VPS ``VPS_ADMIN_USER_IDS`` allowlist (JWT ``sub``).
    2. Signed JWT ``app_metadata.mobi_rent_role`` or ``app_metadata.role`` is ``admin``.

    ``user_metadata``, request JSON, query strings, and the Supabase JWT ``role``
    claim (always ``authenticated``) are ignored.
    """
    uid = str(user_id or "").strip()
    if uid and uid in frozenset(allowlist or ()):
        return _ADMIN
    app_meta = _app_metadata(claims)
    for key in _ADMIN_APP_METADATA_KEYS:
        if str(app_meta.get(key) or "").strip().lower() == _ADMIN:
            return _ADMIN
    return _USER


def is_admin_user(user: AuthUser | None) -> bool:
    return user is not None and str(user.role or "").strip().lower() == _ADMIN


def _app_metadata(claims: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(claims, Mapping):
        return {}
    raw = claims.get("app_metadata")
    return dict(raw) if isinstance(raw, Mapping) else {}
