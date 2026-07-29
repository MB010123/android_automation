"""Composition root / entrypoint for the Mobi-Rent hardware agent daemon.

Run on boot (see systemd/mobi-rent-agent.service). Wires together the
concrete infrastructure adapters and the application use case, then runs
the heartbeat loop until terminated.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from pathlib import Path

from application.heartbeat_service import HeartbeatService, HeartbeatServiceConfig
from application.health_service import HealthService, HealthServiceConfig
from application.provisioning_service import ProvisioningService, ProvisioningServiceConfig
from application.proxy_service import ProxyService, ProxyServiceConfig
from application.retry_policy import RetryPolicy
from application.slot_coordinator import SlotOperationCoordinator
from infrastructure.activation_payload import QrActivationPayloadResolver
from infrastructure.adb_companion import AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.adb_health import AdbDeviceHealthController
from infrastructure.adb_provisioner import AdbCompanionProvisioner
from infrastructure.adb_proxy import AdbVpnProxyConfigurator
from infrastructure.adb_slot_status import AdbSlotStatusProvider, SlotMapError, load_slot_map
from infrastructure.api_client import HttpHeartbeatTransport
from infrastructure.config import AgentConfig, ConfigError, load_config
from infrastructure.logging_setup import configure_logging
from infrastructure.provisioning_api import HttpActivationJobSource
from infrastructure.proxy_routes import ProxyRouteConfigError, load_proxy_routes
from infrastructure.system_clock import SystemClock

logger = logging.getLogger("mobi_rent_agent.main")

SLOT_MAP_PATH = Path(__file__).parent / "slot_map.json"


def build_heartbeat_service(config: AgentConfig, slot_map: dict[int, str]) -> HeartbeatService:
    slot_status_provider = AdbSlotStatusProvider(slot_map=slot_map, adb_path=config.adb_path)
    # When a dedicated heartbeat endpoint is configured (Lovable API route),
    # it authenticates with the hardware agent token; direct Supabase REST
    # uses the anon key.
    auth_key = config.hardware_agent_token if config.heartbeat_endpoint else config.supabase_anon_key
    transport = HttpHeartbeatTransport(
        endpoint=config.queue_endpoint,
        anon_key=auth_key,
        timeout_seconds=config.request_timeout_seconds,
    )

    service_config = HeartbeatServiceConfig(
        hardware_agent_token=config.hardware_agent_token,
        interval_seconds=config.heartbeat_interval_seconds,
    )

    return HeartbeatService(
        config=service_config,
        transport=transport,
        slot_status_provider=slot_status_provider,
        clock=SystemClock(),
        retry_policy=RetryPolicy(),
    )


def build_provisioning_service(
    config: AgentConfig,
    slot_map: dict[int, str],
    coordinator: SlotOperationCoordinator,
) -> ProvisioningService | None:
    if not config.provisioning_endpoint:
        return None

    source = HttpActivationJobSource(
        endpoint=config.provisioning_endpoint,
        hardware_agent_token=config.hardware_agent_token,
        timeout_seconds=config.request_timeout_seconds,
    )
    command_runner = AdbCommandRunner(
        adb_path=config.adb_path,
        timeout_seconds=config.request_timeout_seconds,
    )
    provisioner = AdbCompanionProvisioner(
        command_runner=command_runner,
        companion_socket=config.provisioning_companion_socket,
        provisioning_timeout_seconds=config.provisioning_timeout_seconds,
    )
    payload_resolver = QrActivationPayloadResolver(
        timeout_seconds=config.request_timeout_seconds,
    )
    return ProvisioningService(
        config=ProvisioningServiceConfig(
            poll_interval_seconds=config.provisioning_poll_interval_seconds,
            max_workers=config.provisioning_max_workers,
        ),
        source=source,
        provisioner=provisioner,
        payload_resolver=payload_resolver,
        slot_map=slot_map,
        coordinator=coordinator,
    )


def build_proxy_service(
    config: AgentConfig,
    slot_map: dict[int, str],
    coordinator: SlotOperationCoordinator,
) -> ProxyService | None:
    if not config.proxy_routes_path:
        return None
    routes = load_proxy_routes(config.proxy_routes_path, set(slot_map))
    runner = AdbCommandRunner(config.adb_path, config.request_timeout_seconds)
    client = AdbForwardedJsonClient(
        runner,
        config.network_companion_socket,
        config.request_timeout_seconds,
    )
    return ProxyService(
        config=ProxyServiceConfig(
            reconcile_interval_seconds=config.proxy_reconcile_interval_seconds,
        ),
        configurator=AdbVpnProxyConfigurator(client),
        slot_map=slot_map,
        routes=routes,
        coordinator=coordinator,
    )


def build_health_service(
    config: AgentConfig,
    slot_map: dict[int, str],
    coordinator: SlotOperationCoordinator,
) -> HealthService | None:
    if not config.health_monitor_enabled:
        return None
    runner = AdbCommandRunner(config.adb_path, config.request_timeout_seconds)
    return HealthService(
        config=HealthServiceConfig(
            interval_seconds=config.health_interval_seconds,
            failure_threshold=config.health_failure_threshold,
            reboot_cooldown_seconds=config.health_reboot_cooldown_seconds,
        ),
        controller=AdbDeviceHealthController(runner),
        slot_map=slot_map,
        coordinator=coordinator,
        clock=SystemClock(),
    )


def build_services() -> tuple[
    HeartbeatService,
    ProvisioningService | None,
    ProxyService | None,
    HealthService | None,
]:
    config = load_config()
    configure_logging(config.log_level)
    slot_map = load_slot_map(config.slot_map_path or SLOT_MAP_PATH)
    coordinator = SlotOperationCoordinator(list(slot_map))
    return (
        build_heartbeat_service(config, slot_map),
        build_provisioning_service(config, slot_map, coordinator),
        build_proxy_service(config, slot_map, coordinator),
        build_health_service(config, slot_map, coordinator),
    )


def main() -> int:
    try:
        heartbeat_service, provisioning_service, proxy_service, health_service = build_services()
    except (ConfigError, SlotMapError, ProxyRouteConfigError) as exc:
        # Logging may not be configured yet if config loading itself failed.
        logging.basicConfig(level=logging.ERROR)
        logger.error("Startup failed: %s", exc)
        return 1

    def _handle_shutdown(signum, _frame) -> None:
        logger.info("Received signal %s, shutting down gracefully", signum)
        heartbeat_service.stop()
        if provisioning_service is not None:
            provisioning_service.stop()
        if proxy_service is not None:
            proxy_service.stop()
        if health_service is not None:
            health_service.stop()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    optional_services = [
        ("provisioning-service", provisioning_service),
        ("proxy-service", proxy_service),
        ("health-service", health_service),
    ]
    worker_threads: list[threading.Thread] = []
    for name, service in optional_services:
        if service is None:
            logger.info("%s disabled by configuration", name)
            continue
        thread = threading.Thread(target=service.run_forever, name=name, daemon=True)
        thread.start()
        worker_threads.append(thread)

    heartbeat_service.run_forever()
    for thread in worker_threads:
        thread.join(timeout=5.0)
    logger.info("Heartbeat service stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
