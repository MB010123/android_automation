from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.proxy_service import ProxyService, ProxyServiceConfig
from application.slot_coordinator import SlotOperationCoordinator
from domain.models import ProxyRoute
from domain.ports import ProxyConfigurator
from infrastructure.proxy_routes import ProxyRouteConfigError, load_proxy_routes


class FakeConfigurator(ProxyConfigurator):
    def __init__(self, failing_slots: set[int] | None = None) -> None:
        self.failing_slots = failing_slots or set()
        self.calls: list[tuple[str, ProxyRoute]] = []

    def ensure_route(self, serial: str, route: ProxyRoute) -> None:
        self.calls.append((serial, route))
        if route.slot_id in self.failing_slots:
            raise RuntimeError("route unavailable")


def test_proxy_failure_is_isolated_to_one_slot():
    routes = {
        1: ProxyRoute(1, "proxy-1.example", 1080),
        2: ProxyRoute(2, "proxy-2.example", 1080),
    }
    configurator = FakeConfigurator(failing_slots={1})
    service = ProxyService(
        ProxyServiceConfig(),
        configurator,
        {1: "SERIAL-1", 2: "SERIAL-2"},
        routes,
        SlotOperationCoordinator([1, 2]),
    )

    assert service.run_once() == {1: False, 2: True}


def test_proxy_route_loader_rejects_shared_assignment(tmp_path: Path):
    path = tmp_path / "routes.json"
    path.write_text(
        json.dumps(
            {
                "1": {"host": "same.example", "port": 1080},
                "2": {"host": "same.example", "port": 1080},
            }
        )
    )

    with pytest.raises(ProxyRouteConfigError, match="unique"):
        load_proxy_routes(path, {1, 2})


def test_proxy_route_loader_requires_every_managed_slot(tmp_path: Path):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"1": {"host": "one.example", "port": 1080}}))

    with pytest.raises(ProxyRouteConfigError, match="missing=\\[2\\]"):
        load_proxy_routes(path, {1, 2})
