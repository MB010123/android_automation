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
            {
                "name": "Auth",
                "description": (
                    "Supabase/Lovable Auth is the user identity authority. "
                    "The browser signs up, logs in, and resets passwords through Supabase Auth. "
                    "Optional VPS `/auth/*` routes proxy GoTrue only; the VPS does not store "
                    "passwords or issue a second user JWT. "
                    "`FARM_SERVICE_TOKEN` is Lovable/server → VPS. "
                    "VPS → Lovable tenant calls use a separate server-only machine credential "
                    "that is never accepted on these inbound routes."
                ),
            },
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
                "UserAccessBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "Use `Authorization: Bearer <USER_ACCESS_TOKEN>` from Supabase Auth "
                        "(or the optional VPS GoTrue proxy). "
                        "Never send FARM_SERVICE_TOKEN, FARM_AGENT_API_TOKEN, VOIDFIX_WEBHOOK_SECRET, "
                        "or any VPS↔Lovable machine credential from the browser."
                    ),
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
                "Farm health does not observe radio state; `cellular_status` stays unknown. "
                "`carrier_name` and `imei2` come from the authoritative Supabase `slots` row "
                "when present; otherwise they are null / unknown. Request bodies never supply IMEI2."
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
                "carrier_name": {"type": "string", "nullable": True},
                "imei2": {"type": "string", "nullable": True},
                "imei2_status": {"type": "string", "enum": ["unknown", "known"]},
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
        "AuthCredentialsRequest": {
            "type": "object",
            "required": ["email", "password"],
            "properties": {
                "email": {"type": "string", "format": "email"},
                "password": {"type": "string", "minLength": 12, "maxLength": 128},
            },
        },
        "AuthUser": {
            "type": "object",
            "required": ["id", "email", "email_verified"],
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "email": {"type": "string", "format": "email"},
                "email_verified": {"type": "boolean"},
                "created_at": {"type": "string", "format": "date-time"},
                "last_login_at": {"type": "string", "format": "date-time", "nullable": True},
                "role": {"type": "string", "enum": ["user", "admin"]},
            },
        },
        "AuthSession": {
            "type": "object",
            "required": ["access_token", "token_type", "expires_in"],
            "properties": {
                "access_token": {"type": "string"},
                "token_type": {"type": "string", "enum": ["Bearer"]},
                "expires_in": {"type": "integer"},
            },
        },
        "AuthSuccessResponse": {
            "type": "object",
            "required": ["ok", "user", "session"],
            "properties": {
                "ok": {"type": "boolean", "enum": [True]},
                "user": {"$ref": "#/components/schemas/AuthUser"},
                "session": {"$ref": "#/components/schemas/AuthSession"},
            },
        },
        "AuthMeResponse": {
            "type": "object",
            "required": ["ok", "user"],
            "properties": {
                "ok": {"type": "boolean", "enum": [True]},
                "user": {"$ref": "#/components/schemas/AuthUser"},
            },
        },
        "InvalidCredentialsResponse": {
            "type": "object",
            "required": ["ok", "error"],
            "properties": {
                "ok": {"type": "boolean", "enum": [False]},
                "error": {"type": "string", "enum": ["invalid_credentials"]},
                "message": {"type": "string"},
            },
        },
        "FarmServiceSessionResponse": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "authenticated": {"type": "boolean"},
                "audience": {"type": "string", "enum": ["farm_service", "user"]},
                "token_type": {
                    "type": "string",
                    "enum": ["FARM_SERVICE_TOKEN", "USER_ACCESS_TOKEN"],
                },
                "session_id": {"type": "string", "format": "uuid"},
                "user": {"$ref": "#/components/schemas/AuthUser"},
                "message": {"type": "string"},
            },
        },
        "SlotListResponse": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "count": {"type": "integer"},
                "slots": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/SlotStatusResponse"},
                },
            },
            "required": ["ok", "slots"],
        },
        "SlotRecordResponse": {
            "type": "object",
            "description": (
                "Lovable `public.slots`-shaped view of one farm bay. "
                "`user_id` is the authenticated owner when the slot is claimed. "
                "`carrier_name` and `imei2` are read from Supabase `slots` only. "
                "EID is not a slots column today and is not returned here. "
                "proxy_auth and gateway_api_key are never returned."
            ),
            "properties": {
                "ok": {"type": "boolean"},
                "slot_id": {"type": "string", "format": "uuid"},
                "motherboard_slot_num": {"type": "integer"},
                "hardware_box_id": {"type": "string"},
                "user_id": {"type": "string", "format": "uuid", "nullable": True},
                "rental_id": {"type": "string", "nullable": True},
                "status": {"type": "string"},
                "assigned": {"type": "boolean"},
                "assigned_at": {"type": "string", "format": "date-time", "nullable": True},
                "carrier_name": {"type": "string", "nullable": True},
                "phone_number": {"type": "string", "nullable": True},
                "imei2": {"type": "string", "nullable": True},
                "imei2_status": {"type": "string", "enum": ["unknown", "known"]},
                "last_heartbeat": {"type": "string", "format": "date-time", "nullable": True},
                "band_lock_setting": {"type": "string", "nullable": True},
                "proxy_address": {"type": "string", "nullable": True},
                "provisioning_phase": {"type": "string", "nullable": True},
                "heartbeat": {"type": "string"},
                "adb_online": {"type": "boolean", "nullable": True},
                "checked_at": {"type": "string", "format": "date-time"},
            },
        },
        "DeviceStatusResponse": {
            "type": "object",
            "description": (
                "Customer GET/POST `/rentals/{rental_id}/remote-access/device-status`. "
                "Rental ownership is resolved server-side. Inventory fields are passed through "
                "only from the owned assigned slot: `imei2` (`public.slots.imei2`), `carrier` "
                "(`public.slots.carrier_name`), and `eid` when the rental/slot row already "
                "stores it. TEMPORARY: `phone_number` is the Farm `slot_msisdn_map` MSISDN "
                "for that assigned slot when `SLOT_MSISDN_MAP_PATH` (or the map file) is "
                "present on the VPS; if that slot has no mapping, fall back to "
                "`public.slots.phone_number`. Empty inventory is `null` / `unknown` "
                "(never a placeholder). `cellular_status` is always `unknown` (no approved "
                "live radio reader; not inferred from Wi-Fi/ADB). IMEI1 is not returned. "
                "No GADS URLs, tokens, serials, or workspace IDs."
            ),
            "properties": {
                "ok": {"type": "boolean"},
                "state": {"type": "string", "enum": ["online", "offline", "unavailable"]},
                "online": {"type": "boolean"},
                "adb_online": {"type": "boolean", "nullable": True},
                "remote_access_available": {"type": "boolean"},
                "remote_access_busy": {"type": "boolean"},
                "requires_manual_action": {"type": "boolean"},
                "session_active": {"type": "boolean"},
                "busy": {"type": "boolean"},
                "available": {"type": "boolean"},
                "slot_id": {"type": "integer"},
                "coordinate_space": {"type": "string", "enum": ["native_device_pixels"]},
                "native_resolution": {
                    "type": "object",
                    "nullable": True,
                    "properties": {
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                },
                "native_resolution_unavailable": {"type": "string"},
                "imei2": {"type": "string", "nullable": True},
                "imei2_status": {"type": "string", "enum": ["unknown", "known"]},
                "eid": {"type": "string", "nullable": True},
                "eid_status": {"type": "string", "enum": ["unknown", "known"]},
                "carrier": {"type": "string", "nullable": True},
                "carrier_status": {"type": "string", "enum": ["unknown", "known"]},
                "phone_number": {"type": "string", "nullable": True},
                "phone_number_status": {"type": "string", "enum": ["unknown", "known"]},
                "cellular_status": {"type": "string", "enum": ["unknown"]},
            },
        },
        "EsimAssignRequest": {
            "type": "object",
            "description": (
                "User-owned eSIM assignment. Prefer an internal storage key. "
                "Arbitrary external URLs are rejected unless they match VPS_ESIM_ALLOWED_URL_PREFIXES. "
                "The HTTP handler does not download the file."
            ),
            "required": ["rental_id", "carrier"],
            "properties": {
                "rental_id": {"type": "string", "format": "uuid", "example": EXAMPLE_RENTAL_ID},
                "carrier": {"type": "string"},
                "storage_key": {
                    "type": "string",
                    "description": "Internal object identifier for private storage.",
                },
                "qr_code_url": {
                    "type": "string",
                    "description": "Storage key or allowlisted private-storage URL. Not fetched by the VPS.",
                },
                "esim_qr_url": {
                    "type": "string",
                    "description": "Alias for qr_code_url on farm-service assign.",
                },
                "band_lock": {"type": "string"},
                "proxy": {"type": "string"},
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


_RENTAL_PARAM = {
    "name": "rental_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "format": "uuid"},
    "description": "Lovable rental/slot record id owned by the authenticated customer.",
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
    user_bearer: list[dict[str, list[str]]] = [{"UserAccessBearer": []}]
    user_or_farm: list[dict[str, list[str]]] = [{"UserAccessBearer": []}, {"FarmServiceBearer": []}]
    auth_example = {
        "email": "user@example.com",
        "password": "strong-password-12",
    }

    return {
        "/auth/signup": {
            "post": {
                "tags": ["Auth"],
                "summary": "Create a user account and session",
                "description": (
                    "Optional convenience proxy to Supabase Auth. The browser may also sign up "
                    "directly through Lovable/Supabase Auth. Email is normalized to lowercase. "
                    "Passwords are sent only to Supabase GoTrue and never stored or logged on the VPS. "
                    "A client-supplied `role` is ignored; new accounts are `user`."
                ),
                "security": [],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/AuthCredentialsRequest"},
                            "example": auth_example,
                        }
                    }
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/AuthSuccessResponse"},
                            }
                        }
                    },
                    "400": {"description": "invalid_email, weak_password, invalid_request, invalid_json"},
                    "429": {"description": "rate_limited"},
                    "503": {"description": "auth_not_configured"},
                },
            }
        },
        "/auth/login": {
            "post": {
                "tags": ["Auth"],
                "summary": "Authenticate and issue a user access token",
                "description": (
                    "Failures always return `invalid_credentials` (no user_not_found / wrong_password). "
                    "Bounded temporary lockout after repeated failures."
                ),
                "security": [],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/AuthCredentialsRequest"},
                            "example": auth_example,
                        }
                    }
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/AuthSuccessResponse"},
                            }
                        }
                    },
                    "401": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvalidCredentialsResponse"},
                            }
                        }
                    },
                    "429": {"description": "rate_limited"},
                },
            }
        },
        "/auth/logout": {
            "post": {
                "tags": ["Auth"],
                "summary": "Revoke the current user session",
                "security": user_bearer,
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean", "enum": [True]}},
                                }
                            }
                        }
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                },
            }
        },
        "/auth/me": {
            "get": {
                "tags": ["Auth"],
                "summary": "Current authenticated user",
                "security": user_bearer,
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/AuthMeResponse"},
                            }
                        }
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                },
            }
        },
        "/auth/session": {
            "get": {
                "tags": ["Auth"],
                "summary": "Identify the presented credential class",
                "description": (
                    "USER_ACCESS_TOKEN returns the user session. "
                    "FARM_SERVICE_TOKEN returns audience=farm_service and is never treated as a user. "
                    "FARM_AGENT_API_TOKEN and VOIDFIX_WEBHOOK_SECRET do not authenticate this route."
                ),
                "security": user_or_farm,
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/FarmServiceSessionResponse"},
                            }
                        }
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                },
            }
        },
        "/auth/forgot-password": {
            "post": {
                "tags": ["Auth"],
                "summary": "Request a password-reset token",
                "description": (
                    "Always returns `{ok:true}`. Recovery is issued by Supabase Auth. "
                    "Configure SMTP on the Supabase project; the VPS does not send mail."
                ),
                "security": [],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"email": {"type": "string", "format": "email"}},
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "Generic success (no account enumeration)"}},
            }
        },
        "/auth/reset-password": {
            "post": {
                "tags": ["Auth"],
                "summary": "Consume a one-time reset token",
                "security": [],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["token", "password"],
                                "properties": {
                                    "token": {"type": "string"},
                                    "password": {"type": "string"},
                                },
                            }
                        }
                    }
                },
                "responses": {
                    "200": {"description": "Password updated; existing sessions revoked"},
                    "400": {"description": "invalid_request or weak_password"},
                },
            }
        },
        "/auth/verify-email": {
            "post": {
                "tags": ["Auth"],
                "summary": "Consume a one-time email verification token",
                "security": [],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["token"],
                                "properties": {"token": {"type": "string"}},
                            }
                        }
                    }
                },
                "responses": {
                    "200": {"description": "Email marked verified"},
                    "400": {"description": "invalid_request"},
                },
            }
        },
        "/auth/resend-verification": {
            "post": {
                "tags": ["Auth"],
                "summary": "Issue a new email verification token",
                "description": "Requires a user access token. Does not send email until an SMTP integration is wired.",
                "security": user_bearer,
                "responses": {
                    "200": {"description": "ok"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                },
            }
        },
        "/rentals/{rental_id}/remote-access": {
            "get": {
                "tags": ["Remote access (in-app setup)"],
                "summary": "Current remote-access session for the caller's rental",
                "description": (
                    "Disabled unless `REMOTE_ACCESS_POC_ENABLED=true`. The rental bay must have a unique GADS "
                    "workspace in `gads_workspaces.json` (bays 1–20). The backend verifies "
                    "customer JWT -> rental -> assigned bay -> Farm-mapped device -> that bay's workspace "
                    "server-side; any failure is `403 forbidden`. Never returns platform credentials, serials, "
                    "UDIDs, workspace ids, or GADS admin JWT. "
                    "Includes `activation_state` (REMOTE_ACCESS_READY | DEVICE_REBOOTING | "
                    "CUSTOMER_ACTIVATION_REQUIRED | ACTIVATING | ACTIVE | FAILED), `qr_ready`, and `guidance`. "
                    "When the session is active and not mid-reboot, GET runs the same read-only Farm observation "
                    "as `activation-status` and persists `confirmed`/`partial` for THIS rental only after this "
                    "rental uploaded a QR or prepare-esim became ready. Leftover physical eSIM, a previous "
                    "rental's confirmation, QR upload alone, GADS connect, or phone-online is not Phone Ready. "
                    "eSIM activation is 100% manual by the customer."
                ),
                "security": user_bearer,
                "parameters": [_RENTAL_PARAM],
                "responses": {
                    "200": {"description": "Session view (status none|active|expired|revoked|released)"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "403": {"description": "forbidden"},
                },
            },
            "post": {
                "tags": ["Remote access (in-app setup)"],
                "summary": "Create or reuse temporary remote access for the caller's rental",
                "description": (
                    "Creates a per-rental GADS lease on the assigned Pixel, or reuses a still-valid "
                    "session for the same rental/slot. The browser never receives GADS admin credentials, "
                    "a GADS JWT, hub-ui login, device list, serial/UDID, or unrestricted GADS URLs. "
                    "`stream_path` is a VPS-proxied MJPEG URL on this API. Touch/control goes to POST "
                    "`.../remote-access/control`. The request body is ignored: the browser cannot choose "
                    "a slot or device. The customer still performs Android's real eSIM and default-SMS "
                    "confirmations. This is not eSIM authorization."
                ),
                "security": user_bearer,
                "parameters": [_RENTAL_PARAM],
                "responses": {
                    "200": {"description": "Existing valid session reused"},
                    "201": {"description": "Created; in-app session (`session_mode=in_app`, `stream_path`)"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "403": {"description": "rental_not_owned | session_expired"},
                    "404": {"description": "rental_not_found"},
                    "409": {"description": "remote_access_busy | phone_offline | remote_access_not_ready"},
                    "502": {"description": "gads_unavailable"},
                    "503": {"description": "gads_unavailable | phone_unavailable"},
                },
            },
        },
        "/rentals/{rental_id}/remote-access/{action}": {
            "get": {
                "tags": ["Remote access (in-app setup)"],
                "summary": "Phone stream (`stream`) or device status (`device-status`)",
                "description": (
                    "`GET .../stream` proxies MJPEG (`multipart/x-mixed-replace; boundary=frame` unless GADS "
                    "supplies another boundary) through the VPS. `GET .../device-status` returns customer-facing "
                    "online/offline/unavailable state plus read-only inventory from the owned assigned slot: "
                    "`imei2`, `carrier`, `phone_number`, and `eid` with matching `*_status` (`known`/`unknown`). "
                    "TEMPORARY: `phone_number` prefers Farm `slot_msisdn_map` for the assigned slot "
                    "(`SLOT_MSISDN_MAP_PATH` must be set on the VPS to show numbers); else "
                    "`public.slots.phone_number`. Missing inventory is `null` / `unknown` (no live "
                    "ADB/eUICC probe). `cellular_status` is always `unknown`. The customer's own "
                    "stream is never `remote_access_busy`. Never returns GADS URLs, tokens, serials, "
                    "or workspace IDs."
                ),
                "security": user_bearer,
                "parameters": [
                    _RENTAL_PARAM,
                    {
                        "name": "action",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "enum": ["stream", "device-status"]},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "MJPEG stream or JSON device-status",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/DeviceStatusResponse"},
                            }
                        },
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "403": {"description": "rental_not_owned | session_expired"},
                    "404": {"description": "rental_not_found"},
                    "409": {"description": "remote_access_not_ready | phone_offline | remote_access_busy"},
                    "502": {"description": "gads_unavailable"},
                },
            },
            "post": {
                "tags": ["Remote access (in-app setup)"],
                "summary": "revoke | release | device-status | reboot | prepare-esim | activation-status | control | complete | reconnect-cellular | troubleshoot",
                "description": (
                    "`revoke`: end the caller's platform access. `release` (user or FarmServiceBearer): revoke and "
                    "return the device to the pool when the rental ends. `device-status`: also accepted as POST. "
                    "`control`: tap/swipe/type/back/home/recents/notification_shade/quick_settings/rotate/settings "
                    "in native device pixels "
                    "(swipe accepts `start_x`/`start_y`/`end_x`/`end_y` or `x`/`y`/`x2`/`y2`, optional `duration_ms`). "
                    "Home, Recents, notification shade, Quick Settings, and rotate are semantic actions with no "
                    "keycodes. The semantic `settings` action opens Add eSIM "
                    "(`android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS`), not the Settings homepage. "
                    "Assigned customers have full normal Android access from session start — before, "
                    "during, and after eSIM activation. Home/Recents/shade/QS/rotation, apps, and "
                    "bottom-edge or status-bar swipes are allowed; leaving SIM Settings via Home is not blocked and does "
                    "not auto-recover to the eSIM screen. "
                    "When `CUSTOMER_SESSION_RESTRICTIONS` is on (production default; unset is ON; "
                    "`0`/`false`/`no`/`off` disables), control 200 may include `restricted_destination` "
                    "(`add_esim` or `home`) and `restriction` (`settings_redirected`, `esim_back_to_home`, "
                    "`sensitive_app_blocked`). Flag off does not 502 the settings action and does not launch Add eSIM. "
                    "Keys, ADB, shell, and device identity remain `forbidden_control`. "
                    "Rotate uses GADS orientation (portrait/landscape) only; no ADB wm/overscan. "
                    "`complete`: closes the remote session and runs safe QR cleanup; does not delete eSIM or factory-reset. "
                    "`reconnect-cellular`: customer JWT, owned rental only; Farm `airplane_cycle` is currently "
                    "`501 action_not_supported` (no approved airplane companion command). "
                    "`troubleshoot`: diagnostics from existing device/activation status; optional `action=reboot` "
                    "uses the allowlisted Farm reboot task only. "
                    "`requires_manual_action` never blocks remote access. No `provision_esim`, no EuiccManager."
                ),
                "security": user_or_farm,
                "parameters": [
                    _RENTAL_PARAM,
                    {
                        "name": "action",
                        "in": "path",
                        "required": True,
                        "schema": {
                            "type": "string",
                            "enum": [
                                "revoke",
                                "release",
                                "device-status",
                                "reboot",
                                "prepare-esim",
                                "activation-status",
                                "control",
                                "complete",
                                "reconnect-cellular",
                                "troubleshoot",
                            ],
                        },
                    },
                ],
                "responses": {
                    "200": {"description": "ok"},
                    "202": {"description": "accepted (async reboot / prepare-esim)"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "403": {"description": "forbidden"},
                    "404": {"description": "remote_access_not_found"},
                    "429": {"description": "rate_limited (reconnect-cellular cooldown)"},
                    "501": {"description": "action_not_supported (reconnect-cellular / airplane_cycle)"},
                    "503": {"description": "farm_unreachable | esim_ref_unavailable | remote_access_not_configured"},
                    "504": {"description": "timeout"},
                },
            }
        },
        "/rentals/{rental_id}/esim/upload": {
            "post": {
                "tags": ["Remote access (in-app setup)"],
                "summary": "Upload eSIM QR image onto the rental Pixel Camera",
                "description": (
                    "Customer JWT only. Multipart field `qr_image` (PNG/JPG/WEBP). "
                    "The VPS authenticates the rental owner and requires a Farm-mapped bay "
                    "(not `REMOTE_ACCESS_POC_SLOT_IDS`). Farm `adb push`es "
                    "`/sdcard/DCIM/Camera/mobirent_esim_qr_*.{png|jpg|webp}`. "
                    "Does not require a remote-access session or `prepare-esim`. "
                    "Does not call `assign`, GADS, EuiccManager, or silent provisioning. "
                    "When `CUSTOMER_SESSION_RESTRICTIONS` is on (production default), a successful "
                    "placement also one-shot navigates the authorized rental bay to Add eSIM "
                    "(`android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS`) and may return "
                    "`restricted_destination=add_esim` / `restriction=qr_navigated_to_add_esim`. "
                    "Flag off skips that navigate. Placement still succeeds if navigate fails. "
                    "Request body QR URLs are not used. ADB serial is not returned to the browser."
                ),
                "security": user_bearer,
                "parameters": [_RENTAL_PARAM],
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["qr_image"],
                                "properties": {
                                    "qr_image": {
                                        "type": "string",
                                        "format": "binary",
                                        "description": "PNG, JPEG, or WEBP QR image (max 5 MiB).",
                                    }
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": (
                            "placed; `ok`, `placed`, `job_id`, `destination`, sizes; "
                            "optional `restricted_destination` / `restriction` when Add eSIM navigate ran"
                        )
                    },
                    "400": {"description": "qr_upload_missing | qr_not_an_image"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "403": {"description": "forbidden"},
                    "409": {"description": "device_offline"},
                    "413": {"description": "qr_image_too_large"},
                    "422": {"description": "qr_zero_byte | qr_not_on_device | qr_push_failed"},
                    "503": {"description": "farm_unreachable"},
                },
            }
        },
        "/rentals/{rental_id}/end": {
            "post": {
                "tags": ["Farm management"],
                "summary": "End a rental and release durable occupancy",
                "description": (
                    "Farm-service only. Sequence: revoke GADS (idempotent; already-gone is success), "
                    "record operator device cleanup (`device_cleanup_required`; no factory reset, "
                    "no silent eSIM delete), persist CLEANUP REQUIRED so the bay is not advertised or "
                    "assigned until `POST /farm/slots/{bay}/cleanup-verified`, unclaim the tenant slot, then "
                    "release the durable VPS reservation. Reservation is not released because an "
                    "assign/provision job completed. A rental that has not ended keeps ownership and occupancy."
                ),
                "security": bearer,
                "parameters": [_RENTAL_PARAM],
                "responses": {
                    "200": {"description": "ended; occupancy released (idempotent)"},
                    "400": {"description": "invalid_rental_id"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "409": {"description": "rental_active"},
                    "503": {"description": "auth_unavailable | remote_access_platform_error"},
                },
            }
        },
        "/rentals/{rental_id}/cancel": {
            "post": {
                "tags": ["Remote access (in-app setup)"],
                "summary": "Customer cancel: revoke remote access and start cleanup",
                "description": (
                    "Customer JWT. Immediately revokes the GADS lease, runs safe QR-artifact cleanup "
                    "(no factory reset, no silent eSIM delete), unclaims the tenant, releases occupancy, "
                    "and marks the bay CLEANUP REQUIRED until an administrator verifies the device."
                ),
                "security": user_bearer,
                "parameters": [_RENTAL_PARAM],
                "responses": {
                    "200": {"description": "cancelled; cleanup required"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "403": {"description": "forbidden"},
                    "409": {"description": "rental_active"},
                },
            }
        },
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
        "/farm/slots/{bay}/cleanup-verified": {
            "post": {
                "tags": ["Farm management"],
                "summary": "Admin: CLEANUP REQUIRED → available",
                "description": (
                    "Farm-service only. After Niaozun/GADS/ADB physical verification, clear the durable "
                    "CLEANUP REQUIRED hold so the bay can be assigned again. Rejected while the bay is "
                    "still reserved or assigned. Never performed by a customer session."
                ),
                "security": bearer,
                "parameters": [bay_param],
                "responses": {
                    "200": {"description": "cleanup cleared or already not required"},
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {"description": "slot_not_found"},
                    "409": {"description": "slot_unavailable"},
                },
            }
        },
        "/jobs/{job_id}": {
            "get": {
                "tags": ["Farm management"],
                "summary": "Get async job status",
                "security": user_or_farm,
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
                "security": user_or_farm,
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
        "/slots": {
            "get": {
                "tags": ["Farm management"],
                "summary": "List slots visible to the caller",
                "description": (
                    "USER_ACCESS_TOKEN: only slots owned by that user. "
                    "FARM_SERVICE_TOKEN: every configured bay (operations inventory). "
                    "Cellular/IMEI2 remain unknown unless observed."
                ),
                "security": user_or_farm,
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SlotListResponse"},
                            }
                        }
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                },
            }
        },
        f"/slots/{{slot_id}}": {
            "get": {
                "tags": ["Farm management"],
                "summary": "One slot in Lovable public.slots field names",
                "description": (
                    "VPS `slot_id` is the public farm UUID. "
                    "Users receive 404 for slots they do not own. "
                    "`proxy_auth` and `gateway_api_key` are never returned."
                ),
                "security": user_or_farm,
                "parameters": [slot_param],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SlotRecordResponse"},
                            }
                        }
                    },
                    "401": {"$ref": "#/components/responses/Unauthorized"},
                    "404": {"description": "slot_not_found"},
                },
            }
        },
        f"/slots/{{slot_id}}/esim": {
            "post": {
                "tags": ["Farm management"],
                "summary": "Assign / provision eSIM by public slot UUID",
                "description": (
                    "User JWT: only if `slot_ownership` already exists for that user "
                    "(created by a FARM_SERVICE_TOKEN assignment). Unowned bays return 404. "
                    "Does not download arbitrary URLs. Farm-service callers may use `esim_qr_url` and optional `user_id` to establish ownership."
                ),
                "security": user_or_farm,
                "parameters": [slot_param],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/EsimAssignRequest"},
                        }
                    },
                },
                "responses": {
                    "202": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/JobAcceptedResponse"},
                            }
                        }
                    },
                    "400": {"description": "invalid_rental_id, invalid_request"},
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
                "security": user_or_farm,
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
                "security": user_or_farm,
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
                "security": user_or_farm,
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
                "security": user_or_farm,
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
                "security": user_or_farm,
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
