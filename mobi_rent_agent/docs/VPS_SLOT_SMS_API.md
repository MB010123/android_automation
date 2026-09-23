# VPS Lovable slot SMS API

Server-to-server endpoints on the VPS backend (`tools/vps_backend_server.py`). The browser must **never** receive `FARM_SERVICE_TOKEN`.

## Authentication

All routes below require:

```http
Authorization: Bearer <FARM_SERVICE_TOKEN>
```

Invalid or missing token → **401** `{"error":"unauthorized"}`.

Uses the same constant-time compare as Farm Agent auth (`infrastructure/farm_agent_auth.py`).

## Slot identity

Path `{slot_id}` is a **stable public UUID** for farm bay 1–20 (`infrastructure/slot_public_id.py`, UUID v5). Optional overrides: `SLOT_PUBLIC_ID_MAP_PATH` JSON `{ "<uuid>": 3 }`.

## POST /slots/{slot_id}/sms/send

Enqueue outbound SMS from the given farm bay. **Asynchronous:** returns **202** immediately; dispatch runs in a background thread → `FarmSmsClient` → Farm `POST /agent/sms/send` → existing VoidFix/Android path.

### Request

```json
{
  "to": "+14695550182",
  "body": "Hello from Mobi-Rent",
  "idempotency_key": "uuid"
}
```

### Success (202)

```json
{
  "message_id": "<uuid>",
  "status": "queued",
  "slot_id": "<public-slot-uuid>"
}
```

Duplicate `(farm_slot, idempotency_key)` with same payload → **202** with the same `message_id`. Different payload → **409** `idempotency_conflict`.

### Errors

| HTTP | Body |
|------|------|
| 400 | `invalid_destination`, `invalid_message`, `missing_idempotency_key`, `invalid_json` |
| 401 | `unauthorized` |
| 404 | `slot_not_found` |
| 409 | `device_offline`, `idempotency_conflict` |
| 429 | `rate_limited`, `retry_after` (seconds) |
| 503 | `farm_unreachable`, `service_unavailable` |
| 500 | `internal_error` |

### Rate limits (in-process defaults)

- `VPS_SMS_RATE_LIMIT_PER_SLOT` — default **10** / 60s per bay  
- `VPS_SMS_RATE_LIMIT_GLOBAL` — default **120** / 60s total  

### Persistence

`OUTBOUND_MESSAGES_DB_PATH` (default `logs/outbound_api_messages.sqlite`), table `outbound_api_messages`.

### Status lifecycle

`queued` → `sent` (or `failed`). **Delivered** is not inferred from Farm HTTP acceptance alone; if the Farm response status is `delivered`, it is stored. Otherwise the record reflects Farm/VoidFix final status when returned.

## GET /messages/{message_id}

Returns one outbound message (includes `to` and `body` for server-side polling). **404** if unknown.

## GET /slots/{slot_id}/messages

Query: `limit` (1–100, default 50), `cursor`, optional `direction`.

```json
{
  "messages": [ … ],
  "next_cursor": null
}
```

## Environment

| Variable | Purpose |
|----------|---------|
| `FARM_SERVICE_TOKEN` | Lovable → VPS auth |
| `FARM_AGENT_URL` / `FARM_AGENT_API_TOKEN` | VPS → Farm (unchanged) |
| `OUTBOUND_MESSAGES_DB_PATH` | API message SQLite |
| `WEBHOOK_FARM_DISPATCH_MAX_ATTEMPTS` | Shared retry count for async dispatch |

Webhook auto-reply (`WEBHOOK_AUTO_REPLY_ENABLED`) is independent; leave **false** unless operating inbound auto-replies.
