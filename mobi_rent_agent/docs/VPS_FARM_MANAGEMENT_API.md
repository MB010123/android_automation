# VPS farm management API (Lovable server-side)

Authentication for all endpoints below: `Authorization: Bearer <FARM_SERVICE_TOKEN>` (never expose to browsers).

Error shapes:

- Service-layer errors (all routes in this document): `{ "ok": false, "error": "<code>", "message": "<human text>" }` (rate limits add `retry_after`).
- Authentication gate (`401`): legacy minimal body `{ "error": "unauthorized" }` — no `ok`/`message`. Kept unchanged for compatibility.

## GET /farm/slots/available

Returns bays that are configured, ADB-online (via the live Farm health proxy), not actively assigned, and without a pending/running VPS job. It does **not** consult the last provisioning outcome: a bay whose previous assign ended `failed`/`unsupported` is listed here again once released (see `status = failed` below).

```json
{ "ok": true, "available": [ { "bay": 4, "box": "POD_01", "slot_id": "<public-uuid>" } ] }
```

`FARM_DEFAULT_BOX` env (default `POD_01`).

## POST /farm/slots/{bay}/assign

Async provisioning request. **202** `{ "job_id": "<uuid>" }`.

### Flow

1. VPS validates bay, rental UUID, `esim_qr_url`, and carrier.
2. VPS creates a durable job (`type=assign`) and claims the bay in `slot_assignments`.
3. Slot events: `assignment_requested`, `assignment_started`.
4. Worker calls Farm `POST /agent/tasks/run` with `type=assign` and the safe payload (no caller-supplied ADB serial).
5. Farm resolves serial from `slot_map.json`, enforces `PROVISIONING_ALLOWED_SLOT_IDS`, ADB preflight, then runs the same subscription provisioner as the hardware daemon (`build_subscription_provisioner` → `HumanActivationProvider` or armed `AuthorizedEsimProvider` + companion `provision_esim`).
6. Job **`done`** only when Farm returns `ok: true` (provisioning succeeded). Otherwise **`failed`** with `error=provisioning_failed` and the bay claim is released.

Idempotency: `assign-{rental_id}` — duplicate rental does not enqueue a second provisioning job.

### Prerequisites

- Farm Agent running with `FARM_AGENT_API_TOKEN`, valid `slot_map.json`, and `.env` provisioning flags.
- Live unattended download requires the same gates as `main.py` (`ESIM_LIVE_DOWNLOAD_ARMED`, slot-1-only allowlist, companion, etc.). Default human path completes only when four-layer verification confirms activation; otherwise the job fails honestly.

## GET /jobs/{job_id}

```json
{
  "job_id": "uuid",
  "type": "assign",
  "state": "pending|running|done|failed",
  "progress": 0,
  "error": null
}
```

Jobs persist in `VPS_JOBS_DB_PATH` (default `logs/vps_jobs.sqlite`). Pending jobs are picked up after VPS restart by the background worker. Running jobs at crash time are recovered per the existing worker design (re-process pending; running may be stuck until manual intervention depending on deployment).

## POST /slots/{slot_id}/actions/{action}

Supported action names: `reboot`, `airplane_cycle`, `voidfix_repair`.

**202** `{ "job_id": "<uuid>" }`.

Public `slot_id` is mapped server-side to a farm bay; callers cannot pass ADB serials or VoidFix device IDs.

| Action | Farm Agent | Status |
|--------|------------|--------|
| `reboot` | `adb -s <mapped serial> reboot` | **Supported** |
| `assign` (via bay API) | `SubscriptionProvisioner.provision` via `farm_task_executor._run_assign` | **Supported** (success = real provisioning outcome) |
| `airplane_cycle` | — | **Not supported** — 501 `action_not_supported` (no approved airplane command in `mobi_rent.network` companion; raw `adb shell settings` is not exposed) |
| `voidfix_repair` | — | **Not supported** — 501 `action_not_supported` (VoidFix adapter is SMS send/inbound only; no repair API in repo) |

Farm task allowlist: `reboot`, `assign`, `airplane_cycle`, `voidfix_repair` only. Payload keys such as `serial`, `command`, `voidfix_device_id` are rejected.

## GET /slots/{slot_id}/events

Query: `since` (unix timestamp), `limit` (max 100).

```json
{
  "ok": true,
  "slot_id": "<public-uuid>",
  "events": [
    { "id": 42, "slot_id": "<public-uuid>", "at": "2026-01-01T00:00:00+00:00", "type": "provisioning_failed", "detail": "action_not_supported" }
  ]
}
```

`id` is the store's monotonic integer event ID. Event types include assignment and action lifecycle (`slot_assigned`, `assignment_requested`, `provisioning_started`, `provisioning_completed`/`provisioning_failed`, `assignment_completed`/`assignment_failed`, `{action}_requested`, `{action}_started`, `{action}_completed`/`{action}_failed`), heartbeat transitions (`device_online`/`device_offline`), SMS lifecycle (`sms_send_requested`, `sms_sent`, `sms_failed`) and `inbound_sms_received`. No SMS bodies or tokens.

## GET /slots/{slot_id}/status

Per-slot status derived from three stores (no live Farm call on request):

- **heartbeat** (`SlotStatusStore`, `logs/slot_status.sqlite`): the VPS polls Farm `GET /agent/health` every `VPS_FARM_HEARTBEAT_INTERVAL_SECONDS` (default 30) and records `adb_online` per bay. Farm health only exposes ADB reachability. `heartbeat` is `fresh`, `stale` (> 3× interval), `farm_unreachable`, or `none`.
- **assignment** (`SlotAssignmentStore`): `assigned`, `rental_id`, `assigned_at`.
- **jobs** (`VpsJobStore`): `active_job_id/type`, `last_assign_job_id`, `provisioning_phase`.

`status` derivation order (first match wins): active `assign` job → `provisioning`; other active job → `busy`; heartbeat not fresh → `unknown`; `adb_online=false` → `offline`; assigned and last provisioning `completed` → `online`; assigned and last phase `requires_manual_action` → `requires_manual_action`; assigned otherwise → `assigned`; last assign `provisioning_phase` in {`failed`, `unsupported`} → `failed`; else `available`.

Terminal outcomes and how to read them:

- **`status = failed`** means: the most recent assign job on this bay ended without success (`provisioning_phase` is `failed` or `unsupported`), the assignment was released, and no newer assign job exists. It is *sticky* until the next `POST /farm/slots/{bay}/assign` on that bay. `provisioning_phase` is **not** collapsed: `status: "failed"` with `provisioning_phase: "unsupported"` is the expected pair for an unsupported operation, and the job (`GET /jobs/{id}`) keeps `provisioning_phase: "unsupported"`, `failure_class: "unsupported"`, `error: "action_not_supported"`. Because the bay is released, `GET /farm/slots/available` may list it at the same time; the two endpoints answer different questions ("can I assign this bay now?" vs "what happened to the last assignment?").
- **`requires_manual_action`** is surfaced on the *job* and in `provisioning_phase`, not as a slot `status`: the worker releases the bay on every assign failure, so the slot `status` returns to `available` (heartbeat permitting) while `provisioning_phase` on the status body remains `requires_manual_action` until the next assign. Lovable should key the "user must act" UI off `provisioning_phase`/`failure_class` of the job, not off `status`.
- **`unknown`** is "no data" (no or stale heartbeat, or Farm unreachable), never a failure.

```json
{
  "ok": true, "slot_id": "<uuid>", "bay": 4, "box": "POD_01",
  "status": "online", "assigned": true, "rental_id": "...", "assigned_at": "...",
  "adb_online": true, "last_seen_at": "...", "last_checked_at": "...",
  "heartbeat": "fresh", "heartbeat_interval_seconds": 30,
  "active_job_id": null, "active_job_type": null,
  "last_assign_job_id": "<uuid>", "provisioning_phase": "completed",
  "cellular_status": "unknown", "carrier": null,
  "imei2": null, "imei2_status": "unknown",
  "checked_at": "..."
}
```

`cellular_status`, `carrier`, `imei2`, `imei2_status` are always `unknown`/`null`: the Farm health endpoint does not observe radio, carrier, or IMEI2, and the VPS never infers them from ADB state. Heartbeat transitions emit `device_online` / `device_offline` slot events.

## Inbound SMS → Lovable (external)

This repo implements the **VPS outbound client**, not the Lovable receiver.

After `POST /voidfix/inbound`, VPS normalizes and optionally POSTs to `LOVABLE_INBOUND_WEBHOOK_URL` with header `X-Mobi-Rent-Signature` (HMAC-SHA256 of body, secret `LOVABLE_INBOUND_WEBHOOK_HMAC_SECRET`).

**Lovable must implement** `POST /api/public/farm/inbound-sms` and verify the signature.

Normalized payload:

```json
{
  "slot_id": "<public-slot-uuid>",
  "from": "+...",
  "to": null,
  "body": "...",
  "received_at": "...",
  "provider_message_id": "..."
}
```

Duplicate VoidFix deliveries are suppressed via unique `provider_message_id` on `inbound_messages`.

## Rate limits

Management routes (`/farm/slots/*`, `/slots/*/actions/*`, `/slots/*/status`, `/slots/*/events`, `/jobs/*`): `VPS_MGMT_RATE_LIMIT_PER_SLOT` (default 20/min per bay), `VPS_MGMT_RATE_LIMIT_GLOBAL` (default 200/min).
SMS routes (`/slots/*/sms/send`, `/messages/*`): `VPS_SMS_RATE_LIMIT_PER_SLOT` (default 10/min per bay), `VPS_SMS_RATE_LIMIT_GLOBAL` (default 120/min).
Exceeded limits return `429 rate_limited`. Limits are in-process (per VPS service instance).

## Security

- Lovable → VPS: `FARM_SERVICE_TOKEN`
- VPS → Farm: `FARM_AGENT_API_TOKEN`
- Slot isolation: serial and VoidFix IDs come from on-disk maps only.
- No secrets in logs; SMS bodies are not stored in slot events.
