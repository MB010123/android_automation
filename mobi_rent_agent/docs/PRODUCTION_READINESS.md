# Production readiness — VPS / Farm backend

Evidence-based status for the Lovable → VPS (`https://api.loanerphones.com`) → Farm Agent → Android journey.

Status vocabulary:

- **READY** — implemented, covered by automated tests, and (where noted) verified against production.
- **PARTIALLY READY** — implemented and tested, but the real-world outcome depends on a gate or capability not exercisable in automated tests.
- **UNSUPPORTED** — the API exists and returns `501 action_not_supported`; no safe existing mechanism in the repo.
- **EXTERNAL** — not hosted on the VPS (Lovable / Supabase / VoidFix side).
- **NOT VERIFIED** — code exists but no production verification has been performed from this workstation (no VPS SSH access during this work).

## Capability matrix

| Capability | Implementation | Automated test evidence | Production verification | External dependency | Status / remaining action |
|---|---|---|---|---|---|
| VPS health `GET /health` | `tools/vps_backend_server.py` | `test_vps_backend_*` | Verified earlier (public 200) | — | **READY** |
| Farm connectivity proxy `GET /farm/status` | `_farm_status` | mocked tests | Verified 20/20 ADB online earlier | Farm Agent on Windows PC | **READY** |
| Lovable → VPS auth (`FARM_SERVICE_TOKEN`) | `farm_agent_auth.authorize_farm_request` | `test_vps_slot_status::test_http_status_requires_auth_and_returns_body`, mgmt/sms tests | User confirmed token configured; **do not rotate** | Lovable secret store | **READY** |
| Slot availability `GET /farm/slots/available` | `VpsFarmManagementService.list_available` | `test_vps_farm_management_api` | Verified 200 earlier | — | **READY** |
| Bay assignment `POST /farm/slots/{bay}/assign` | `assign_slot` (202 + job, idempotent per `rental_id`) | idempotent replay, 409 on other rental, response shape, failure releases bay | **NOT VERIFIED** (real device action) | — | **READY** (API) / provisioning outcome see below |
| Async jobs `GET /jobs/{job_id}` | `VpsJobStore`, `VpsJobWorker` | pending→running→done/failed; `failure_class`; `provisioning_phase` matrix (`test_vps_slot_status`, `test_vps_api_contract`) | **NOT VERIFIED** | — | **READY** |
| Slot status `GET /slots/{slot_id}/status` | `get_slot_status` + `SlotStatusStore` + `FarmHeartbeatPoller` | 26 tests: online/offline/provisioning/busy/failed/unknown/stale/farm_unreachable/manual/404/auth | **NOT VERIFIED** | Farm `GET /agent/health` | **READY** (ADB-level truth); radio/IMEI2 always `unknown` |
| Heartbeat | VPS polls Farm health every `VPS_FARM_HEARTBEAT_INTERVAL_SECONDS` (30s); stale after 3× | transition events emitted once; farm error preserves last state | **NOT VERIFIED** | Farm Agent reachable from VPS | **READY** |
| Slot state machine | `vps_slot_state.py` (`ALLOWED_TRANSITIONS`, `derive_slot_state`) | `test_state_machine_transitions`, `test_derive_priority_rules` | n/a (derived, never caller-set) | — | **READY** |
| Farm task allowlist / isolation | `farm_agent_tasks`, `farm_task_executor` | `test_farm_agent_tasks` (forbidden keys, serial from slot_map only, 501s) | Farm Agent deploy pending | Farm Agent | **READY** (code) — needs Farm Agent redeploy |
| Reboot `POST /slots/{id}/actions/reboot` | Farm `reboot` task | `test_status_busy_during_reboot`, mgmt tests | **NOT VERIFIED** (real device action) | Farm Agent | **READY** |
| Airplane cycle | 501 `action_not_supported` | `test_farm_agent_tasks` | — | Android companion command missing | **UNSUPPORTED** |
| VoidFix repair | 501 `action_not_supported` | `test_farm_agent_tasks` | — | VoidFix has no repair API in repo | **UNSUPPORTED** |
| eSIM provisioning (unattended) | `_run_assign` → `build_subscription_provisioner` | human path → `requires_manual_action`; armed path only via env gates | **NOT VERIFIED** | `ESIM_LIVE_DOWNLOAD_ARMED`, allowlist, real QR | **PARTIALLY READY** — default outcome is `requires_manual_action`, never fake success |
| Post-provisioning verification (cellular, carrier, IMEI2) | Not observable via Farm health | status API asserts `cellular_status`/`imei2_status` == `unknown` | — | Would need a new read-only Farm endpoint (`adb_health` exists in daemon only) | **PARTIALLY READY** — reported as `unknown`, not inferred |
| Slot events `GET /slots/{id}/events` | `SlotEventStore` | assignment/action/SMS/heartbeat events | **NOT VERIFIED** | — | **READY** |
| Outbound SMS `POST /slots/{id}/sms/send` | `VpsSlotSmsService` (idempotent, async, events) | `test_vps_slot_sms_api` | Controlled slot-1 test earlier this project | VoidFix / Farm | **READY** |
| Inbound VoidFix webhook `POST /voidfix/inbound` | listener + dedupe | valid JSON/form, malformed 400, missing field, wrong secret 401, missing secret 401, duplicate one dispatch | Real payload verified earlier | VoidFix webhook config | **READY** |
| Inbound → Lovable forward | HMAC `X-Mobi-Rent-Signature` client | `lovable_inbound_webhook` tests | **NOT VERIFIED** | `LOVABLE_INBOUND_WEBHOOK_URL/_HMAC_SECRET`, Lovable receiver | **PARTIALLY READY** — receiver is Lovable's |
| Hardware queue `POST /api/public/hardware/queue` | consumed by `main.py` daemon | daemon tests | — | Lovable/Supabase | **EXTERNAL** |
| OpenAPI / Swagger (`/docs`, `/openapi.json`, `/redoc`) | `vps_openapi_spec.py` | `test_vps_openapi` (paths, schemas, no secrets) | — | — | **READY** |
| Rate limiting | `VpsRateLimiter` (in-process) | mgmt + sms tests | — | — | **READY** (single instance only) |
| Observability | structured log lines `farm_task_completed/failed`, heartbeat transitions as events | — | — | — | **PARTIALLY READY** — no metrics endpoint |

## Slot state rules

States: `available`, `assigned`, `provisioning`, `requires_manual_action`, `online`, `offline`, `busy`, `network_error`, `failed`, `unknown`.

`derive_slot_state` priority (first match wins):

1. active `assign` job → `provisioning`
2. any other active job → `busy`
3. heartbeat not fresh (none / stale / farm unreachable) → `unknown`
4. `adb_online == false` → `offline`
5. assigned and last provisioning `completed` → `online`
6. assigned and last phase `requires_manual_action` → `requires_manual_action`
7. assigned otherwise → `assigned`
8. last assign job `provisioning_phase` in {`failed`, `unsupported`} (`TERMINAL_FAILED_PHASES`) → `failed`
9. else → `available`

`network_error` is reserved and never inferred (no radio signal available). `ALLOWED_TRANSITIONS` documents legal moves; `can_transition` is available for callers that need to reject illegal transitions.

Terminal-outcome semantics (decided, documented, tested in `test_vps_slot_status`):

- **`failed` is sticky and coexists with `provisioning_phase = unsupported`.** `status: failed` says "the last assignment on this bay ended without success and no newer assign job exists". The phase is never rewritten: an unsupported operation keeps `provisioning_phase: unsupported` and `failure_class: unsupported` on the job and on the status body, while `status` is `failed`. It clears only when a new assign job is created for the bay.
- **Availability and status intentionally disagree after a failure (E2, accepted).** `GET /farm/slots/available` answers "can this bay be assigned now?" using live Farm ADB state + assignment + active jobs only; the failed bay was released, so it is listed. `GET /slots/{slot_id}/status` answers "what happened last?" and reports `failed`. Lovable should use availability for bay selection and status/job for outcome display. No hidden retry or auto-clear exists.
- **`requires_manual_action` is a job/phase outcome, not a reachable slot status (E3, accepted).** The worker releases the bay on every assign failure, so `is_assigned` is false after a manual-action outcome and rule 6 cannot fire; the slot `status` returns to `available`/`offline`/`unknown` per heartbeat while `provisioning_phase: requires_manual_action` remains on the status body and job. Rule 6 is kept for the future case where an assignment is retained through a manual step. Lovable must drive "user must act" from `provisioning_phase`/`failure_class`, never from `status`.

## Heartbeat rules

- Source: Farm `GET /agent/health` (`ok`, `slot_count`, `adb_online`, `offline_slots`). No new Farm endpoint required.
- Poller: `FarmHeartbeatPoller` on the VPS, interval `VPS_FARM_HEARTBEAT_INTERVAL_SECONDS` (default 30).
- Persistence: `logs/slot_status.sqlite` (`last_checked_at`, `last_seen_at` only advances when online, `farm_ok`, `farm_error`).
- Freshness: fresh if `now - last_checked_at <= 3 × interval` and Farm was reachable.
- Events: `device_online` / `device_offline` on transitions only (no spam per poll).
- Farm unreachable: last ADB state preserved, `heartbeat = farm_unreachable`, status `unknown`.

## Provisioning lifecycle

`provisioning_phase` on assign jobs: `queued` (pending) → `provisioning` (running) → `completed` | `failed` | `requires_manual_action` | `unsupported`.

`failure_class` on failed jobs: `unsupported` (`action_not_supported`), `requires_manual_action` (human/LPA/Settings path), `temporary` (`farm_unreachable`, timeouts), `permanent` (everything else). On any assign failure the bay is released.

Truthfulness guarantees: `state: done` / `provisioning_phase: completed` is emitted only when the Farm Agent returned `ok: true` for the assign task; an `unsupported` or failed Farm response is never converted to success, and the response envelope `ok: true` on `GET /jobs/{id}` means only "job found" — the outcome is in `state`/`provisioning_phase`/`failure_class`.

## Lovable server-side contract

Base URL: `https://api.loanerphones.com`. Auth header `Authorization: Bearer <FARM_SERVICE_TOKEN>` on every route except `/health`, `/docs`, `/openapi.json`, `/redoc`, `/voidfix/inbound`.

**Frontend security boundary:** Browser → Lovable server-side function (holds `FARM_SERVICE_TOKEN`) → VPS. The token must never be shipped to the browser, embedded in client JS, or logged.

| Step | Request | Success | Notes |
|---|---|---|---|
| 1 | `GET /farm/slots/available` | 200 `{ok, available:[{bay, box, slot_id}]}` | READ-ONLY |
| 2 | `POST /farm/slots/{bay}/assign` `{rental_id, esim_qr_url, carrier, band_lock?, proxy?}` | 202 `{ok, job_id, bay, slot_id, status:"pending"}` | same `rental_id` → same `job_id`; 409 if bay held by another rental; 429 rate limit |
| 3 | `GET /jobs/{job_id}` | 200 `{state, provisioning_phase, failure_class?, error, message, ...}` | poll until `done`/`failed` |
| 4 | `GET /slots/{slot_id}/status` | 200 `SlotStatusResponse` | drives UI badge; treat `unknown` as "no data", not failure |
| 5 | `GET /slots/{slot_id}/events?since=&limit=` | 200 `{ok, slot_id, events:[{id, slot_id, at, type, detail}]}` | audit trail |
| 6 | `POST /slots/{slot_id}/actions/reboot` `{idempotency_key}` | 202 job | REAL DEVICE ACTION |
| 7 | `POST /slots/{slot_id}/actions/airplane_cycle` / `voidfix_repair` | 501 `action_not_supported` | show as unsupported in UI |
| 8 | `POST /slots/{slot_id}/sms/send` `{to, body, idempotency_key}` | 202 `{message_id, ...}` | REAL SMS |
| 9 | `GET /messages/{message_id}` | 200 | delivery state |
| 10 | Inbound: Lovable receives HMAC-signed POST at its own `/api/public/farm/inbound-sms` | Lovable returns 2xx | EXTERNAL receiver |

Error body shape (service layer): `{ "ok": false, "error": "<code>", "message": "<human text>" }`. Exception: the authentication gate returns the legacy minimal `401 { "error": "unauthorized" }` (no `ok`/`message`); this is intentional and unchanged for compatibility.

## Shutdown

`tools/vps_backend_server.py` stops background threads before closing stores: `FarmHeartbeatPoller.stop(join_timeout=5)` → `VpsJobWorker.stop(join_timeout=5)` → close SQLite stores. Joins are bounded (`SHUTDOWN_JOIN_TIMEOUT_SECONDS = 5.0` per thread); threads are daemons, so shutdown never blocks indefinitely.

## Automated test status

`pytest tests/ -q` → **497 passed, 1 skipped, 1 failed**.

The single failure is `tests/test_slot_isolation.py::test_allowlist_does_not_mutate_slot_map_json`. It pins the SHA-256 of the **local, gitignored** `slot_map.json` (`mobi_rent_agent/.gitignore:5`). On this workstation that file was last modified before this work started and its digest differs from the pinned production digest. It is environment-specific (guards a production file this repo does not version) and is unrelated to the changes here. It must not be deleted; it will pass on a machine whose `slot_map.json` matches the pinned production map.

## Manual verification plan (production)

| Step | Class | Command / action | Expected |
|---|---|---|---|
| Health | SAFE / READ-ONLY | `curl https://api.loanerphones.com/health` | 200 |
| Docs | SAFE / READ-ONLY | open `/docs`, `/openapi.json` | 200, `/slots/{slot_id}/status` present |
| Farm status | SAFE / READ-ONLY | `GET /farm/status` with bearer | 200, `adb_online` count |
| Available | SAFE / READ-ONLY | `GET /farm/slots/available` | 200 |
| Slot status | SAFE / READ-ONLY | `GET /slots/<uuid>/status` for an idle bay | `heartbeat: fresh` after ≥1 poll interval; `status: available` or `offline` |
| Heartbeat file | SAFE / READ-ONLY | `ls -l /opt/mobi-rent-agent/logs/slot_status.sqlite` | exists after restart + 30s |
| Assign + poll | REAL DEVICE ACTION | operator approval; expect `requires_manual_action` unless eSIM gates armed | truthful phase |
| Reboot | REAL DEVICE ACTION | operator approval; watch `device_offline` → `device_online` events | events present |
| SMS send | REAL SMS | operator approval only | `message_id`, delivery |
| Inbound | REAL SMS | controlled VoidFix test only | `inbound_sms_received` event, Lovable receipt |

## Deployment (VPS)

Flow: GitHub → `/tmp/mobi-rent-agent-deploy` (clone) → selective copy → `/opt/mobi-rent-agent` → `sudo systemctl restart mobi-rent-vps-backend`. **Never** `git reset --hard` in `/opt/mobi-rent-agent`. **Never overwrite:** `.env`, `slot_msisdn_map.json`, `voidfix_devices.json`, `slot_map.json`, `logs/*.sqlite`.

Files changed for the VPS backend (copy these):

- `application/vps_api_contract.py`, `application/vps_slot_state.py`, `application/farm_heartbeat_poller.py`
- `application/vps_farm_management_service.py`, `application/vps_job_worker.py`, `application/vps_slot_sms_service.py`, `application/vps_lovable_routes.py`
- `infrastructure/slot_status_store.py`, `infrastructure/vps_job_store.py`, `infrastructure/slot_assignment_store.py`, `infrastructure/slot_event_store.py`
- `infrastructure/vps_openapi_spec.py`, `infrastructure/vps_api_docs.py`
- `tools/vps_backend_server.py`
- optional env: `VPS_FARM_HEARTBEAT_INTERVAL_SECONDS=30` (default applies if absent; `.env` not modified by deploy)

New runtime file created automatically: `logs/slot_status.sqlite` (next to `vps_jobs.sqlite`).

## Farm Agent dependency

Farm Agent (Windows PC) files changed in this project and requiring a separate Farm deploy + restart of `farm_agent_status_server`:

- `application/farm_agent_tasks.py`, `application/farm_task_types.py`, `application/farm_task_executor.py`
- `infrastructure/subscription_provisioner_factory.py`
- `tools/farm_agent_status_server.py`, `main.py`

Until the Farm Agent is redeployed, `assign` jobs will fail with whatever the old Farm task endpoint returns; the VPS reports that truthfully as a failed job and releases the bay.

## Remaining dependencies / blockers

- **Unattended eSIM**: Android companion + env gates (`ESIM_LIVE_DOWNLOAD_ARMED`, allowlist) — outside this change.
- **Radio / carrier / IMEI2 truth**: requires a new read-only Farm endpoint; until then `unknown`.
- **Airplane / VoidFix repair**: capability gaps, 501.
- **Lovable receiver + secrets**: `LOVABLE_INBOUND_WEBHOOK_URL`, `LOVABLE_INBOUND_WEBHOOK_HMAC_SECRET`, receiver implementation.
- **Hardware queue**: Lovable/Supabase endpoint.
- **Production verification**: no VPS SSH from this workstation; the read-only steps above must be run by an operator.
