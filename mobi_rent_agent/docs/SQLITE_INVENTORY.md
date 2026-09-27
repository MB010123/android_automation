# SQLite inventory after Supabase auth migration

Supabase is the source of truth for identity and tenant data. SQLite remains only for VPS operational state.

## Removed from the production path (AUTH / TENANT)

| Former SQLite | Classification | Replacement |
|---|---|---|
| `vps_auth.sqlite` (`VPS_AUTH_DB_PATH`) | AUTH + TENANT | Unused. `VpsAuthStore` is not constructed by `vps_backend_server.py` |
| `users`, `profiles`, `sessions` | AUTH | Supabase `auth.users` + `profiles` |
| `password_reset_tokens`, `email_verify_tokens` | AUTH | Supabase Auth recover / verify |
| `slot_ownership` | TENANT | Supabase `slots.user_id` |
| `esim_uploads` (SQLite) | TENANT | Supabase `esim_uploads` + `esim-records` |

## Remaining SQLite (operational only)

| File / table | Classification | Why it stays |
|---|---|---|
| `vps_jobs.sqlite` / `vps_jobs` | JOB QUEUE | Farm assign/reboot worker queue, retries, idempotency |
| `slot_assignments.sqlite` / `slot_assignments` | JOB QUEUE | Bay claim while a VPS assign job is in flight |
| `slot_events.sqlite` / `slot_events` | OPERATIONAL CACHE | Device/job event log for `GET /slots/{id}/events` |
| `slot_status.sqlite` / `slot_heartbeats` | OPERATIONAL CACHE | Last Farm ADB heartbeat (not tenant ownership) |
| `outbound_api_messages.sqlite` | JOB QUEUE | SMS dispatch retries / idempotency |
| `outbound_jobs.sqlite` | JOB QUEUE | Webhook auto-reply dispatch |
| `inbound_messages.sqlite` | OPERATIONAL CACHE | VoidFix webhook dedupe; tenant log is also written to Supabase `messages` |
| `sms_outbox.sqlite` | JOB QUEUE | Farm-PC SMS outbox (physical agent, not VPS tenant DB) |

Tenant-facing message history is Lovable/Supabase `messages`, reached through the Lovable server API. The outbound SQLite row is temporary execution state. The VPS does not use `SUPABASE_SERVICE_ROLE_KEY` for these reads.
