"""Load strict one-to-one slot-to-SOCKS5 route assignments."""
from __future__ import annotations

import json
from pathlib import Path

from domain.models import ProxyRoute


class ProxyRouteConfigError(RuntimeError):
    pass


def load_proxy_routes(path: str | Path, managed_slot_ids: set[int]) -> dict[int, ProxyRoute]:
    path = Path(path)
    if not path.exists():
        raise ProxyRouteConfigError(f"Proxy route file not found: {path}")
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProxyRouteConfigError(f"Invalid proxy route file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProxyRouteConfigError("Proxy route file must be a slot-id object")

    routes: dict[int, ProxyRoute] = {}
    try:
        for raw_slot_id, raw_route in raw.items():
            slot_id = int(raw_slot_id)
            if not isinstance(raw_route, dict):
                raise TypeError(f"route for slot {slot_id} must be an object")
            routes[slot_id] = ProxyRoute(
                slot_id=slot_id,
                host=str(raw_route["host"]),
                port=int(raw_route["port"]),
                username=(
                    str(raw_route["username"])
                    if raw_route.get("username") is not None
                    else None
                ),
                password=(
                    str(raw_route["password"])
                    if raw_route.get("password") is not None
                    else None
                ),
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProxyRouteConfigError(f"Invalid proxy route: {exc}") from exc

    configured_slots = set(routes)
    if configured_slots != managed_slot_ids:
        missing = sorted(managed_slot_ids - configured_slots)
        extra = sorted(configured_slots - managed_slot_ids)
        raise ProxyRouteConfigError(f"Proxy routes must exactly match managed slots; missing={missing}, extra={extra}")

    route_keys = [route.route_key for route in routes.values()]
    if len(set(route_keys)) != len(route_keys):
        raise ProxyRouteConfigError("Each slot must have a unique SOCKS5 endpoint/account assignment")
    return routes
