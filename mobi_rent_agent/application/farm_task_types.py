"""Shared Farm task request/result types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_TASK_TYPES = {"reboot", "assign", "airplane_cycle", "voidfix_repair"}


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
