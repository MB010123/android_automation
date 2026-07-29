"""Configuration loading for the agent.

Reads from environment variables (with a `.env` file supported via
python-dotenv) so secrets like the hardware agent token never need to be
hard-coded or committed to source control.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class AgentConfig:
    supabase_url: str
    supabase_anon_key: str
    supabase_table: str
    hardware_agent_token: str
    heartbeat_interval_seconds: float
    request_timeout_seconds: float
    log_level: str
    adb_path: str
    slot_map_path: str | None = None
    heartbeat_endpoint: str | None = None
    provisioning_endpoint: str | None = None
    provisioning_poll_interval_seconds: float = 10.0
    provisioning_timeout_seconds: float = 180.0
    provisioning_companion_socket: str = "mobi_rent.provisioning"
    provisioning_max_workers: int = 20
    proxy_routes_path: str | None = None
    network_companion_socket: str = "mobi_rent.network"
    proxy_reconcile_interval_seconds: float = 30.0
    health_monitor_enabled: bool = False
    health_interval_seconds: float = 15.0
    health_failure_threshold: int = 3
    health_reboot_cooldown_seconds: float = 300.0

    @property
    def queue_endpoint(self) -> str:
        if self.heartbeat_endpoint:
            return self.heartbeat_endpoint
        return f"{self.supabase_url.rstrip('/')}/rest/v1/{self.supabase_table}"


def load_config(env_file: str | None = ".env") -> AgentConfig:
    if load_dotenv is not None and env_file:
        load_dotenv(env_file, override=False)

    supabase_url = os.getenv("SUPABASE_URL")
    if not supabase_url:
        raise ConfigError(
            "SUPABASE_URL is not set. Provide it via environment variable "
            "or a .env file (see .env.example)."
        )

    anon_key = os.getenv("SUPABASE_ANON_KEY")
    if not anon_key:
        raise ConfigError(
            "SUPABASE_ANON_KEY is not set. Provide it via environment variable "
            "or a .env file (see .env.example)."
        )

    token = os.getenv("HARDWARE_AGENT_TOKEN")
    if not token:
        raise ConfigError(
            "HARDWARE_AGENT_TOKEN is not set. Provide it via environment variable "
            "or a .env file (see .env.example)."
        )

    try:
        return AgentConfig(
            supabase_url=supabase_url,
            supabase_anon_key=anon_key,
            supabase_table=os.getenv("SUPABASE_TABLE", "hardware_queue"),
            hardware_agent_token=token,
            heartbeat_interval_seconds=float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "15")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            adb_path=os.getenv("ADB_PATH", "adb"),
            slot_map_path=os.getenv("SLOT_MAP_PATH") or None,
            heartbeat_endpoint=os.getenv("HEARTBEAT_ENDPOINT") or None,
            provisioning_endpoint=os.getenv("PROVISIONING_ENDPOINT") or None,
            provisioning_poll_interval_seconds=float(os.getenv("PROVISIONING_POLL_INTERVAL_SECONDS", "10")),
            provisioning_timeout_seconds=float(os.getenv("PROVISIONING_TIMEOUT_SECONDS", "180")),
            provisioning_companion_socket=os.getenv(
                "PROVISIONING_COMPANION_SOCKET",
                "mobi_rent.provisioning",
            ),
            provisioning_max_workers=int(os.getenv("PROVISIONING_MAX_WORKERS", "20")),
            proxy_routes_path=os.getenv("PROXY_ROUTES_PATH") or None,
            network_companion_socket=os.getenv("NETWORK_COMPANION_SOCKET", "mobi_rent.network"),
            proxy_reconcile_interval_seconds=float(os.getenv("PROXY_RECONCILE_INTERVAL_SECONDS", "30")),
            health_monitor_enabled=_read_bool("HEALTH_MONITOR_ENABLED", False),
            health_interval_seconds=float(os.getenv("HEALTH_INTERVAL_SECONDS", "15")),
            health_failure_threshold=int(os.getenv("HEALTH_FAILURE_THRESHOLD", "3")),
            health_reboot_cooldown_seconds=float(os.getenv("HEALTH_REBOOT_COOLDOWN_SECONDS", "300")),
        )
    except ValueError as exc:
        raise ConfigError(f"Invalid numeric or boolean configuration: {exc}") from exc


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")
