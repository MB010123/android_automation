# VoidFix inbound webhook → Farm SMS dispatch

## Overview

```
VoidFix → POST /voidfix/inbound (VPS)
       → parse + InboundMessageStore
       → WebhookReplyPolicy (optional)
       → OutboundJobStore + FarmSmsClient
       → Tailscale → Farm Agent POST /agent/sms/send
       → SmsDispatchService.send_for_slot → VoidFix → device
```

Farm Agent is **not** exposed on the public internet. VPS reaches it via `FARM_AGENT_URL` (Tailscale).

## VPS: `POST /voidfix/inbound`

Same as before for validation errors (400/401/422). On success: HTTP 200 with `stored_ids` and optional `dispatch` array.

Webhook secret: query `?secret=`, or headers `X-Webhook-Secret` / `X-Voidfix-Webhook-Secret` when `VOIDFIX_WEBHOOK_SECRET` is set.

## Automated reply policy

Off by default.

| Variable | Default | Meaning |
|----------|---------|---------|
| `WEBHOOK_AUTO_REPLY_ENABLED` | `false` | Must be true to dispatch outbound SMS |
| `WEBHOOK_AUTO_REPLY_RULES` | empty | Rules when enabled |
| `WEBHOOK_AUTO_REPLY_BODY_PREFIX` | empty | Optional prefix on reply body |
| `WEBHOOK_FARM_DISPATCH_TIMEOUT_SECONDS` | `120` | VPS → Farm HTTP timeout |
| `WEBHOOK_FARM_DISPATCH_MAX_ATTEMPTS` | `3` | Retries (same idempotency key) |
| `OUTBOUND_JOBS_DB_PATH` | `logs/outbound_jobs.sqlite` | VPS outbound job SQLite |

Rule formats:

- JSON: `{"rules":[{"inbound_slot":1,"sender_slot":1,"reply_to_slot":2}]}`
- Segments: `inbound:1->send:1->to:2` (semicolon-separated)

Only matching **inbound slot** triggers a reply. No rule → inbound stored only.

## Farm Agent: `POST /agent/sms/send`

**Authentication:** `Authorization: Bearer <FARM_AGENT_API_TOKEN>` (server-side only on VPS).

**Request JSON:**

```json
{
  "job_id": "uuid",
  "idempotency_key": "stable-key",
  "sender_slot_id": 1,
  "to_slot_id": 2,
  "body": "message text"
}
```

Alternatively `"slot_id"` for sender, or `"to": "+1..."` instead of `to_slot_id`.

**Responses:**

| HTTP | Meaning |
|------|---------|
| 200 | Send completed or duplicate idempotency (see `duplicate`) |
| 400 | Validation / missing MSISDN map |
| 401 | Missing or invalid Bearer token |
| 422 | Send failed (allowlist, VoidFix error, etc.) |
| 503 | SMS stack not configured |

Example success body:

```json
{
  "ok": true,
  "job_id": "...",
  "idempotency_key": "...",
  "status": "sent",
  "provider_message_id": "3979816",
  "duplicate": false,
  "error": null
}
```

`status` reflects outbox/VoidFix final state when available (`queued`, `accepted`, `sending`, `sent`, `delivered`, `failed`). HTTP acceptance does **not** mean delivered.

## Idempotency

- Inbound event ID: `voidfix:<ID>` when VoidFix provides `ID`, else `hash:<sha256-prefix>` from device/from/body/time.
- VPS `outbound_jobs.inbound_event_id` is UNIQUE.
- Farm uses the same `idempotency_key` in the SMS outbox; retries reuse the key so a second physical send is not issued.

## Failure behavior

- Invalid inbound payload → webhook error response; nothing stored.
- Valid inbound + dispatch failure → inbound row kept; outbound job `failed` with `error` (retryable on next manual/process if you re-drive with same event id — duplicate guard prevents double SMS).
- Farm unreachable → `failed` after retries; no token or secrets in responses/logs/DB.

## Farm configuration

- `SLOT_MSISDN_MAP_PATH` — JSON map `"2": "+1..."` for `to_slot_id` resolution on the farm host. Start from the tracked template `slot_msisdn_map.example.json`; the real `slot_msisdn_map.json` is runtime configuration, gitignored, and must be preserved (never overwritten) by deployments.
- Existing VoidFix allowlist and `VOIDFIX_*` send gates unchanged.
