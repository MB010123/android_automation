"""Shared Farm task request/result types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_TASK_TYPES = {
    "reboot",
    "assign",
    "airplane_cycle",
    "voidfix_repair",
    "remote_access_place_qr",
    "remote_access_activation_status",
    "setup_session_inspect",
    "setup_session_input",
    "setup_session_voidfix_cycle",
    "setup_session_safe_cleanup",
    "device_display_size",
}


@dataclass
class FarmTaskRequest:
    job_id: str
    task_type: str
    farm_slot_id: int
    payload: dict[str, Any]


@dataclass
class FarmTaskResult:
    ok: bool
    http_status: int
    error: str | None = None
    message: str | None = None
    install_state: str | None = None
    activation_code_sent: bool = False
    details: dict[str, Any] | None = None
