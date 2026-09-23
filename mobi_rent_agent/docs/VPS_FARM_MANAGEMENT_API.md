# VPS farm management API (Lovable server-side)

Authentication for all endpoints below: `Authorization: Bearer <FARM_SERVICE_TOKEN>` (never expose to browsers).

## GET /farm/slots/available

Returns bays that are configured, ADB-online (via Farm health proxy), not actively assigned, and without a pending/running VPS job.

```json
{ "available": [ { "bay": 4, "box": "POD_01" } ] }
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

Event types include assignment and action lifecycle (`assignment_requested`, `assignment_completed`, `assignment_failed`, `{action}_started`, `{action}_completed`, `{action}_failed`). No SMS bodies or tokens.

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

`VPS_MGMT_RATE_LIMIT_PER_SLOT` (default 20/min), `VPS_MGMT_RATE_LIMIT_GLOBAL` (default 200/min).

## Security

- Lovable → VPS: `FARM_SERVICE_TOKEN`
- VPS → Farm: `FARM_AGENT_API_TOKEN`
- Slot isolation: serial and VoidFix IDs come from on-disk maps only.
- No secrets in logs; SMS bodies are not stored in slot events.
