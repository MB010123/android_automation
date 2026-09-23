"""Farm Agent task execution (ADB actions; no SMS)."""

from __future__ import annotations



from typing import Any



from application.farm_task_executor import FarmTaskExecutorDeps, execute_controlled_farm_task, reject_forbidden_payload_keys

from application.farm_task_types import (

    SUPPORTED_TASK_TYPES,

    FarmTaskRequest,

    FarmTaskResult,

)

from infrastructure.config import AgentConfig



__all__ = [

    "SUPPORTED_TASK_TYPES",

    "FarmTaskRequest",

    "FarmTaskResult",

    "parse_farm_task_body",

    "execute_farm_task",

]





def parse_farm_task_body(data: dict[str, Any]) -> FarmTaskRequest:

    job_id = str(data.get("job_id") or "").strip()

    task_type = str(data.get("type") or data.get("task") or "").strip()

    raw_slot = data.get("farm_slot_id")

    if not job_id or not task_type or raw_slot is None:

        raise ValueError("job_id, type, and farm_slot_id are required")

    farm_slot_id = int(raw_slot)

    if not 1 <= farm_slot_id <= 20:

        raise ValueError("farm_slot_id out of range")

    if task_type not in SUPPORTED_TASK_TYPES:

        raise ValueError("unsupported task type")

    payload = data.get("payload")

    if payload is None:

        payload = {}

    if not isinstance(payload, dict):

        raise ValueError("payload must be object")

    reject_forbidden_payload_keys(payload)

    return FarmTaskRequest(

        job_id=job_id,

        task_type=task_type,

        farm_slot_id=farm_slot_id,

        payload=payload,

    )





def execute_farm_task(

    *,

    adb_path: str,

    slot_map: dict[int, str],

    request: FarmTaskRequest,

    agent_config: AgentConfig | None = None,

    deps: FarmTaskExecutorDeps | None = None,

) -> FarmTaskResult:

    return execute_controlled_farm_task(

        adb_path=adb_path,

        slot_map=slot_map,

        request=request,

        agent_config=agent_config,

        deps=deps,

    )

