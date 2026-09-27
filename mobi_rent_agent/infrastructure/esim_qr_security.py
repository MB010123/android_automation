"""Allowlist and SSRF checks for eSIM QR fetch URLs.

Storage keys (no scheme) are references, not fetch targets.
Decoded LPA activation codes are never accepted as URLs.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from application.auth_service import validate_esim_storage_key

_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.google",
        "metadata",
    }
)
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")


def extract_authoritative_esim_ref(row: dict | None) -> str | None:
    """Return Lovable's documented eSIM reference, or None.

    Contract fields (first match wins). Do not invent additional names
    at call sites. If none are present, the caller must fail closed.

    Documented on GET /slots/{id} and GET /slots/by-bay/{bay}:
    ``esim_storage_key``, ``qr_code_url``, ``esim_qr_url``, ``storage_key``.
    """
    if not isinstance(row, dict):
        return None
    for key in ("esim_storage_key", "qr_code_url", "esim_qr_url", "storage_key"):
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return None


def validate_authoritative_esim_ref(
    value: str,
    *,
    allowed_url_prefixes: tuple[str, ...] = (),
) -> str | None:
    """Accept a storage key or an allowlisted, non-private HTTPS URL."""
    key = validate_esim_storage_key(value, allowed_url_prefixes=allowed_url_prefixes)
    if key is None:
        return None
    if "://" not in key:
        return key
    if not esim_fetch_url_is_safe(key, allowed_url_prefixes=allowed_url_prefixes):
        return None
    return key


def esim_fetch_url_is_public_https(url: str) -> bool:
    """HTTPS URL whose host is not loopback, private, link-local, or metadata."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.strip().lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTS:
        return False
    if any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        return False
    return not _host_is_blocked_ip(host)


def esim_fetch_url_is_safe(url: str, *, allowed_url_prefixes: tuple[str, ...] = ()) -> bool:
    """True only for public HTTPS URLs whose prefix is explicitly allowlisted."""
    if not esim_fetch_url_is_public_https(url):
        return False
    prefixes = tuple(p for p in allowed_url_prefixes if p)
    if not prefixes:
        return False
    return any(url.startswith(prefix) for prefix in prefixes)


def _looks_like_ipv6(host: str) -> bool:
    return ":" in host


def _host_is_blocked_ip(host: str) -> bool:
    candidates = {host}
    try:
        for info in socket.getaddrinfo(host, None):
            addr = info[4][0]
            if addr:
                candidates.add(addr)
    except (OSError, socket.gaierror):
        pass
    for candidate in candidates:
        if _ip_is_blocked(candidate):
            return True
    return False


def _ip_is_blocked(raw: str) -> bool:
    text = raw.strip().strip("[]")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or ip == ipaddress.ip_address("169.254.169.254")
    )
