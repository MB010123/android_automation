"""OpenAPI 3.0 specification for the VPS backend (tools/vps_backend_server.py)."""
from __future__ import annotations

from typing import Any

OPENAPI_VERSION = "3.0.3"
API_TITLE = "Mobi-Rent VPS Backend API"
API_VERSION = "1.0.0"

EXAMPLE_SLOT_ID = "a1b2c3d4-e5f6-4789-a012-3456789abcde"
EXAMPLE_MESSAGE_ID = "b2c3d4e5-f6a7-4890-b123-456789abcdef0"
EXAMPLE_JOB_ID = "c3d4e5f6-a7b8-4901-c234-567890abcdef1"
EXAMPLE_RENTAL_ID = "00000000-0000-4000-8000-000000000099"


def build_vps_openapi_document(*, voidfix_webhook_path: str = "/voidfix/inbound") -> dict[str, Any]:
    """Return OpenAPI 3.0.3 document matching the current VPS HTTP handler."""
    webhook_path = voidfix_webhook_path if voidfix_webhook_path.startswith("/") else f"/{voidfix_webhook_path}"
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": API_TITLE,
            "version": API_VERSION,
            "description": (
                "HTTP API served by `tools/vps_backend_server.py` (stdlib `http.server`). "
                "Farm Agent `/agent/*` routes are **not** part of this surface; VPS calls them server-side. "
                "Lovable-hosted routes such as `GET/POST /api/public/hardware/queue` and "
                "`POST /api/public/farm/inbound-sms` are **external** and documented here only as integration notes."
            ),
        },
        "tags": [
            {"name": "Health", "description": "Unauthenticated health and farm proxy."},
            {"name": "Farm management", "description": "Slots, assignment, jobs, device actions, events."},
            {"name": "SMS", "description": "Lovable-facing outbound SMS (async dispatch)."},
            {"name": "Inbound", "description": "VoidFix webhook receiver on the VPS."},
            {"name": "Integration", "description": "Outbound webhooks to Lovable (not inbound VPS routes)."},
        ],
        "paths": _paths(webhook_path),
        "components": {
            "securitySchemes": {
                "FarmServiceBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Use `Authorization: Bearer <FARM_SERVICE_TOKEN>` (server-side only).",
                },
                "VoidfixWebhookSecretHeader": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Voidfix-Webhook-Secret",
                    "description": (
                        "Optional shared secret when `VOIDFIX_WEBHOOK_SECRET` is configured. "
                        "Also accepted: `X-Webhook-Secret` header or `?secret=` query parameter."
                    ),
                },
            },
            "schemas": _schemas(),
            "responses": _shared_responses(),
        },
    }


def _schemas() -> dict[str, Any]:
    return {
        "ErrorResponse": {
            "type": "object",
            "description": (
                "Service-layer error shape (management, jobs, status, events, SMS): "
                "always `ok: false`, an `error` code and a human `message`. "
                "Two legacy edge responses omit `ok`/`message` and carry only `error`: "
                "the authentication gate (`401 {\"error\":\"unauthorized\"}`) and a few "
                "handler-level validation errors (e.g. `invalid_limit` on messages listing)."
            ),
            "properties": {
                "ok": {"type": "boolean", "enum": [False]},
                "error": {"type": "string"},
                "message": {"type": "string"},
                "retry_after": {"type": "number", "description": "Seconds (rate limit)."},
                "detail": {"type": "string"},
            },
            "required": ["error"],
        },
        "AuthErrorResponse": {
            "type": "object",
            "description": "Legacy authentication gate shape; intentionally minimal and unchanged.",
            "properties": {"error": {"type": "string", "enum": ["unauthorized"]}},
            "required": ["error"],
        },
        "HealthResponse": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "role": {"type": "string", "example": "vps"},
                "service": {"type": "string"},
                "webhook_path": {"type": "string"},
                "inbound_stored": {"type": "integer"},
            },
        },
        "FarmStatusResponse": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "role": {"type": "string"},
                "farm": {"type": "object", "additionalProperties": True},
                "error": {"type": "string"},
                "detail": {"type": "string"},
            },
        },
        "SlotAvailabilityList": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "available": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/SlotAvailability"},
                },
            },
            "required": ["ok", "available"],
        },
        "SlotAvailability": {
            "type": "object",
            "properties": {
                "bay": {"type": "integer", "minimum": 1, "maximum": 20},
                "box": {"type": "string", "example": "POD_01"},
                "slot_id": {"type": "string", "format": "uuid"},
            },
            "required": ["bay", "box", "slot_id"],
        },
        "AssignmentRequest": {
            "type": "object",
            "required": ["rental_id", "esim_qr_url", "carrier"],
            "properties": {
                "rental_id": {
                    "type": "string",
                    "format": "uuid",
                    "example": EXAMPLE_RENTAL_ID,
                },
                "esim_qr_url": {
                    "type": "string",
                    "format": "uri",
                    "example": "https://example.test/esim/qr.png",
                },
                "carrier": {"type": "string", "example": "example-carrier"},
                "band_lock": {"type": "string"},
                "proxy": {"type": "string"},
            },
        },
        "JobAcceptedResponse": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "example": True},
                "job_id": {"type": "string", "format": "uuid", "example": EXAMPLE_JOB_ID},
                "bay": {"type": "integer", "example": 4},
                "slot_id": {"type": "string", "format": "uuid", "example": EXAMPLE_SLOT_ID},
                "status": {"type": "string", "example": "pending"},
                "action": {"type": "string", "description": "Present for device actions only."},
            },
            "required": ["ok", "job_id", "status"],
            "description": (
                "Async acceptance. Poll `GET /jobs/{job_id}` until `state` is `done` or `failed`. "
                "Assign idempotency: `assign-{rental_id}` per bay."
            ),
        },
        "JobResponse": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "job_id": {"type": "string", "format": "uuid"},
                "type": {
                    "type": "string",
                    "enum": ["assign", "reboot", "airplane_cycle", "voidfix_repair"],
                },
                "state": {
                    "type": "string",
                    "enum": ["pending", "running", "done", "failed"],
                },
                "status": {"type": "string", "description": "Same as state."},
                "progress": {"type": "integer", "minimum": 0, "maximum": 100},
                "bay": {"type": "integer"},
                "slot_id": {"type": "string", "format": "uuid"},
                "error": {"type": "string", "nullable": True},
                "message": {"type": "string", "nullable": True},
                "provisioning_phase": {
                    "type": "string",
                    "enum": [
                        "queued",
                        "provisioning",
                        "completed",
                        "failed",
                        "requires_manual_action",
                        "unsupported",
                        "unknown",
                    ],
                },
                "failure_class": {
                    "type": "string",
                    "enum": ["unsupported", "requires_manual_action", "temporary", "permanent"],
                    "description": "Present only when state is failed.",
                },
                "created_at": {"type": "string", "format": "date-time"},
                "started_at": {"type": "string", "format": "date-time", "nullable": True},
                "completed_at": {"type": "string", "format": "date-time", "nullable": True},
            },
        },
        "ActionRequest": {
            "type": "object",
            "properties": {
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional; deduplicates jobs per `(type, farm_slot_id, key)` when set.",
                },
            },
        },
        "SmsSendRequest": {
            "type": "object",
            "required": ["to", "body", "idempotency_key"],
            "properties": {
                "to": {"type": "string", "example": "+15551234567"},
                "body": {"type": "string", "example": "Test message"},
                "idempotency_key": {"type": "string", "example": "example-001"},
            },
        },
        "SmsEnqueueResponse": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "format": "uuid"},
                "status": {"type": "string", "example": "queued"},
                "slot_id": {"type": "string", "format": "uuid"},
            },
        },
        "SmsMessageResponse": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "format": "uuid"},
                "slot_id": {"type": "string", "format": "uuid"},
                "direction": {"type": "string", "example": "out"},
                "to": {"type": "string"},
                "body": {"type": "string"},
                "status": {"type": "string"},
                "error_code": {"type": "string", "nullable": True},
                "created_at": {"type": "string", "format": "date-time"},
                "updated_at": {"type": "string", "format": "date-time"},
            },
        },
        "SmsMessageListResponse": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/SmsMessageResponse"},
                },
                "next_cursor": {"type": "string", "nullable": True},
            },
        },
        "SlotEvent": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "slot_id": {"type": "string", "format": "uuid"},
                "at": {"type": "string", "format": "date-time"},
                "type": {"type": "string"},
                "detail": {"type": "string"},
            },
        },
        "SlotStatusResponse": {
            "type": "object",
            "description": (
                "Derived from the VPS heartbeat store (Farm `GET /agent/health` polled every "
                "`heartbeat_interval_seconds`), the assignment store and the job store. "
                "Radio/carrier/IMEI2 are not observed by the Farm health endpoint and are always "
                "reported as unknown/null — never inferred from ADB state."
            ),
            "properties": {
                "ok": {"type": "boolean"},
                "slot_id": {"type": "string", "format": "uuid"},
                "bay": {"type": "integer"},
                "box": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
                        "available",
                        "assigned",
                        "provisioning",
                        "requires_manual_action",
                        "online",
                        "offline",
                        "busy",
                        "network_error",
                        "failed",
                        "unknown",
                    ],
                },
                "assigned": {"type": "boolean"},
                "rental_id": {"type": "string", "nullable": True},
                "assigned_at": {"type": "string", "format": "date-time", "nullable": True},
                "adb_online": {"type": "boolean", "nullable": True},
                "last_seen_at": {"type": "string", "format": "date-time", "nullable": True},
                "last_checked_at": {"type": "string", "format": "date-time", "nullable": True},
                "heartbeat": {"type": "string", "enum": ["fresh", "stale", "farm_unreachable", "none"]},
                "heartbeat_interval_seconds": {"type": "number"},
                "active_job_id": {"type": "string", "nullable": True},
                "active_job_type": {"type": "string", "nullable": True},
                "last_assign_job_id": {"type": "string", "nullable": True},
                "provisioning_phase": {
                    "type": "string",
                    "nullable": True,
                    "enum": [
                        "queued",
                        "provisioning",
                        "completed",
                        "failed",
                        "requires_manual_action",
                        "unsupported",
                        "unknown",
                    ],
                },
                "cellular_status": {"type": "string", "enum": ["unknown"]},
                "carrier": {"type": "string", "nullable": True},
                "imei2": {"type": "string", "nullable": True},
                "imei2_status": {"type": "string", "enum": ["unknown"]},
                "checked_at": {"type": "string", "format": "date-time"},
            },
        },
        "SlotEventsResponse": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "slot_id": {"type": "string", "format": "uuid"},
                "events": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/SlotEvent"},
                    "description": "Each event carries its integer `id` (monotonic per store) and the public `slot_id`.",
                },
            },
            "required": ["ok", "slot_id", "events"],
        },
        "VoidfixInboundSuccess": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "parsed_count": {"type": "integer"},
                "device_ids": {"type": "array", "items": {"type": "string"}},
                "mapped_slots": {
                    "type": "array",
                    "items": {"type": "integer", "nullable": True},
                },
                "stored_ids": {"type": "array", "items": {"type": "integer"}},
                "dispatch": {"type": "array", "items": {"type": "object"}},
            },
        },
        "LovableInboundNormalizedPayload": {
            "type": "object",
            "description": (
                "Payload VPS POSTs to `LOVABLE_INBOUND_WEBHOOK_URL` (external), "
                "signed with `X-Mobi-Rent-Signature` (HMAC-SHA256 hex of raw JSON body). "
                "Secret: `<LOVABLE_INBOUND_WEBHOOK_HMAC_SECRET>` on both sides."
            ),
            "properties": {
                "slot_id": {"type": "string", "format": "uuid", "nullable": True},
                "from": {"type": "string"},
                "to": {"type": "string", "nullable": True},
                "body": {"type": "string"},
                "received_at": {"type": "string", "format": "date-time"},
                "provider_message_id": {"type": "string", "nullable": True},
            },
        },
    }


def _shared_responses() -> dict[str, Any]:
    return {
        "Unauthorized": {
            "description": (
                "Missing or invalid `FARM_SERVICE_TOKEN`. Body is the legacy minimal shape "
                "(`ok`/`message` are absent)."
            ),
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/AuthErrorResponse"},
                    "example": {"error": "unauthorized"},
                }
            },
        },
        "FarmUnreachable": {
            "description": (
                "Farm Agent unreachable or not configured. Always `ok: false` + `error`. "
                "Service-layer routes (503) add `message`; the `/farm/status` proxy (502) adds `detail` instead."
            ),
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                    "example": {"ok": False, "error": "farm_unreachable"},
                }
            },
        },
    }


def _paths(webhook_path: str) -> dict[str, Any]:
    slot_param = {
        "name": "slot_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
        "example": EXAMPLE_SLOT_ID,
    }
    bay_param = {
        "name": "bay",
        "in": "path",
        "required": True,
        "schema": {"type": "integer", "minimum": 1, "maximum": 20},
        "example": 4,
    }
    action_param = {
        "name": "action",
        "in": "path",
        "required": True,
        "schema": {
            "type": "string",
            "enum": ["reboot", "airplane_cycle", "voidfix_repair"],
        },
        "description": (
            "`reboot` is supported end-to-end. "
            "`airplane_cycle` and `voidfix_repair` enqueue async jobs but the Farm Agent "
            "currently returns `action_not_supported` (501) — poll the job; expect `failed` with that error."
        ),
    }
    bearer: list[dict[str, list[str]]] = [{"FarmServiceBearer": []}]

    return {
        "/health": {
            "get": {
                "tags": ["Health"],
                "summary": "VPS health",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/HealthResponse"},
                            }
                        },
                    }
                },
            }
        },
        "/farm/status": {
            "get": {
                "tags": ["Health"],
                "summary": "Proxy Farm Agent health",
                "description": "Calls Farm `GET /agent/health` with `FARM_AGENT_API_TOKEN` (server-side).",
                "responses": {
                    "200": {
                        "description": "Farm status wrapped",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/FarmStatusResponse"},
                            }
                        },
                    },
                    "502": {"$ref": "#/components/responses/FarmUnreachable"},
                    "503": {
                        "description": "Farm Agent URL/token not configured",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                                "example": {"ok": False, "error": "farm_agent_not_configured"},
                            }
                        },
                    },
                },
            }
        },
        "/farm/slots/available": {
            "get": {
                "tags": ["Farm management"],
                "summary": "List assignable bays",
                "security": bearer,
                "responses": {
                    "200": {
                        "description": "Available bays",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SlotAvailabilityList"},
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "503": {"$ref": "#/components/responses/FarmUnreachable"},
                },
            }
        },
        "/farm/slots/{bay}/assign": {
            "post": {
                "tags": ["Farm management"],
                "summary": "Assign bay (async provisioning)",
                "description": (
                    "Accepts **202** and returns `job_id`. Worker runs Farm `POST /agent/tasks/run` with `type=assign`. "
                    "Idempotency: `assign-{rental_id}` — same rental returns the existing job. "
                    "Poll `GET /jobs/{job_id}`: `pending` → `running` → `done` or `failed` "
                    "(e.g. `provisioning_failed` when eSIM path cannot complete)."
                ),
                "security": bearer,
                "parameters": [bay_param],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/AssignmentRequest"},
                        }
                    },
                },
                "responses": {
                    "202": {
                        "description": "Job accepted",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/JobAcceptedResponse"},
                            }
                        },
                    },
                    "400": {
                        "description": "invalid_rental_id, invalid_request, invalid_json",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {
                        "description": "slot_not_found (unknown bay)",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                            }
                        },
                    },
                    "409": {
                        "description": "slot_unavailable, device_offline",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                                "example": {"error": "device_offline"},
                            }
                        },
                    },
                    "429": {
                        "description": "rate_limited",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                            }
                        },
                    },
                    "503": {"$ref": "#/components/responses/FarmUnreachable"},
                },
            }
        },
        "/jobs/{job_id}": {
            "get": {
                "tags": ["Farm management"],
                "summary": "Get async job status",
                "security": bearer,
                "parameters": [
                    {
                        "name": "job_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                        "example": EXAMPLE_JOB_ID,
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Job record",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/JobResponse"},
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {
                        "description": "job_not_found",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                            }
                        },
                    },
                },
            }
        },
        f"/slots/{{slot_id}}/actions/{{action}}": {
            "post": {
                "tags": ["Farm management"],
                "summary": "Enqueue device action (async)",
                "description": (
                    "**202** with `job_id` only — action runs on Farm via `/agent/tasks/run`. "
                    "Poll job status. `airplane_cycle` / `voidfix_repair` may end `failed` with `action_not_supported`."
                ),
                "security": bearer,
                "parameters": [slot_param, action_param],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ActionRequest"},
                        }
                    }
                },
                "responses": {
                    "202": {
                        "description": "Job accepted",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/JobAcceptedResponse"},
                            }
                        },
                    },
                    "400": {
                        "description": "unsupported_action, invalid_json",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {"description": "slot_not_found"},
                    "409": {"description": "slot_unavailable, device_offline"},
                    "429": {"description": "rate_limited"},
                    "503": {"$ref": "#/components/responses/FarmUnreachable"},
                },
            }
        },
        f"/slots/{{slot_id}}/status": {
            "get": {
                "tags": ["Farm management"],
                "summary": "Per-slot status (heartbeat + assignment + job)",
                "description": (
                    "Status of one rented slot, not the global farm. `status` is derived deterministically: "
                    "active assign job → `provisioning`; other active job → `busy`; no fresh heartbeat → `unknown`; "
                    "ADB unreachable → `offline`; assigned + provisioning completed → `online`; "
                    "assigned + manual step → `requires_manual_action`; assigned otherwise → `assigned`; "
                    "last assign `provisioning_phase` in {failed, unsupported} → `failed` (sticky until the next "
                    "assign; `provisioning_phase` is preserved, so `status: failed` + `provisioning_phase: unsupported` "
                    "coexist); else `available`. `requires_manual_action` is surfaced via `provisioning_phase`, "
                    "not `status`, because the bay is released on assign failure. "
                    "Heartbeat is stale after 3× the poll interval or when the Farm Agent is unreachable."
                ),
                "security": bearer,
                "parameters": [slot_param],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SlotStatusResponse"},
                            }
                        }
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {"description": "slot_not_found"},
                },
            }
        },
        f"/slots/{{slot_id}}/events": {
            "get": {
                "tags": ["Farm management"],
                "summary": "List slot events",
                "security": bearer,
                "parameters": [
                    slot_param,
                    {
                        "name": "since",
                        "in": "query",
                        "schema": {"type": "number"},
                        "description": "Unix timestamp (float).",
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                    },
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SlotEventsResponse"},
                            }
                        }
                    },
                    "400": {"description": "invalid_since, invalid_limit"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {"description": "slot_not_found"},
                },
            }
        },
        f"/slots/{{slot_id}}/sms/send": {
            "post": {
                "tags": ["SMS"],
                "summary": "Enqueue outbound SMS",
                "description": (
                    "**202** — message is queued; dispatch is async to Farm Agent. "
                    "Same `(farm_slot, idempotency_key)` with identical `to`+`body` returns the existing message."
                ),
                "security": bearer,
                "parameters": [slot_param],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/SmsSendRequest"},
                        }
                    },
                },
                "responses": {
                    "202": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SmsEnqueueResponse"},
                            }
                        }
                    },
                    "400": {
                        "description": (
                            "missing_idempotency_key, invalid_destination, invalid_message, invalid_json"
                        ),
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {"description": "slot_not_found"},
                    "409": {"description": "device_offline, idempotency_conflict"},
                    "429": {"description": "rate_limited"},
                    "500": {"description": "internal_error"},
                    "503": {"description": "farm_unreachable"},
                },
            }
        },
        f"/messages/{{message_id}}": {
            "get": {
                "tags": ["SMS"],
                "summary": "Get outbound message",
                "security": bearer,
                "parameters": [
                    {
                        "name": "message_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                        "example": EXAMPLE_MESSAGE_ID,
                    }
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SmsMessageResponse"},
                            }
                        }
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {"description": "not_found"},
                },
            }
        },
        f"/slots/{{slot_id}}/messages": {
            "get": {
                "tags": ["SMS"],
                "summary": "List outbound messages for slot",
                "security": bearer,
                "parameters": [
                    slot_param,
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                    },
                    {
                        "name": "cursor",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": "Opaque cursor `created_at|message_id` from prior `next_cursor`.",
                    },
                    {
                        "name": "direction",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": "Optional filter passed to the message store.",
                    },
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SmsMessageListResponse"},
                            }
                        }
                    },
                    "400": {"description": "invalid_limit, invalid_cursor"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {"description": "slot_not_found"},
                },
            }
        },
        webhook_path: {
            "get": {
                "tags": ["Inbound"],
                "summary": "Webhook hint",
                "responses": {
                    "200": {
                        "description": "Indicates POST is expected for inbound SMS.",
                    }
                },
            },
            "post": {
                "tags": ["Inbound"],
                "summary": "VoidFix inbound SMS webhook",
                "description": (
                    "Parses VoidFix webhook body (JSON or form). "
                    "When `VOIDFIX_WEBHOOK_SECRET` is set, require matching secret via header or query. "
                    "Does **not** use `FARM_SERVICE_TOKEN`."
                ),
                "security": [{"VoidfixWebhookSecretHeader": []}],
                "parameters": [
                    {
                        "name": "secret",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": "Alternative to webhook secret headers.",
                    }
                ],
                "requestBody": {
                    "description": "VoidFix provider payload (see VoidFix dashboard docs).",
                    "content": {
                        "application/json": {"schema": {"type": "object"}},
                        "application/x-www-form-urlencoded": {"schema": {"type": "object"}},
                    },
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/VoidfixInboundSuccess"},
                            }
                        }
                    },
                    "400": {"description": "Invalid body"},
                    "401": {"description": "Webhook secret mismatch"},
                    "422": {"description": "SmsGatewayError"},
                },
            },
        },
        "/integration/lovable-inbound-sms": {
            "post": {
                "tags": ["Integration"],
                "summary": "(External) Lovable inbound SMS receiver",
                "description": (
                    "**Not implemented on the VPS.** Lovable hosts e.g. "
                    "POST /api/public/farm/inbound-sms. "
                    "The VPS outbound client posts normalized JSON with header "
                    "X-Mobi-Rent-Signature (HMAC-SHA256 of body using "
                    "the configured inbound webhook HMAC secret on both sides). "
                    "This path is documentation-only in OpenAPI."
                ),
                "servers": [{"url": "https://your-lovable-app.example"}],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/LovableInboundNormalizedPayload"
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "Receiver-specific (Lovable)."}},
            }
        },
    }
