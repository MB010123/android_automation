"""ADB adapter for the privileged full-device VPN/SOCKS5 companion."""
from __future__ import annotations

from domain.models import ProxyRoute
from domain.ports import ProxyConfigurator
from infrastructure.adb_companion import AdbForwardedJsonClient


class AdbVpnProxyConfigurator(ProxyConfigurator):
    def __init__(self, client: AdbForwardedJsonClient) -> None:
        self._client = client

    def ensure_route(self, serial: str, route: ProxyRoute) -> None:
        response = self._client.request(
            serial,
            {
                "command": "ensure_socks5_route",
                "slot_id": route.slot_id,
                "host": route.host,
                "port": route.port,
                "username": route.username,
                "password": route.password,
                "persistent": True,
                "block_on_disconnect": True,
            },
        )
        if response.get("success") is not True:
            error = response.get("error")
            raise RuntimeError(str(error) if error else "VPN companion rejected the SOCKS5 route")
        if response.get("active") is not True:
            raise RuntimeError("VPN companion did not confirm an active route")
        reported_slot = response.get("slot_id")
        if reported_slot is not None and reported_slot != route.slot_id:
            raise RuntimeError("VPN companion confirmed a route for the wrong slot")
