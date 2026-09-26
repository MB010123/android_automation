"""Classify Bearer credentials: user JWT vs FARM_SERVICE_TOKEN. Never mix trust domains."""
from __future__ import annotations

from application.auth_service import AuthContext, AuthService
from infrastructure.farm_agent_auth import authorize_farm_request, extract_bearer_token


def resolve_auth_context(
    *,
    authorization_header: str | None,
    cookie_header: str | None,
    cookie_name: str | None,
    farm_service_token: str | None,
    auth_service: AuthService | None,
) -> AuthContext | None:
    token = extract_bearer_token(authorization_header)
    if not token and cookie_name and cookie_header:
        token = _cookie_value(cookie_header, cookie_name)
    if not token:
        return None
    if authorize_farm_request(token, farm_service_token):
        return AuthContext(kind="farm_service")
    if auth_service is None:
        return None
    return auth_service.validate_user_token(token)


def _cookie_value(header: str, name: str) -> str | None:
    for part in header.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip() == name:
            return value.strip() or None
    return None
