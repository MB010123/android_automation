# VPS user authentication

The VPS (`https://api.loanerphones.com`) is the farm/orchestration API. **Supabase Auth (Lovable Cloud) is the only identity authority.** The VPS does not issue a second user JWT and does not store passwords, password hashes, sessions, or reset tokens.

```
Browser
  → Lovable / Supabase Auth (signup, login, logout, forgot/reset password)
  → Authorization: Bearer <SUPABASE_ACCESS_TOKEN>
  → VPS validates the token (HS256 with SUPABASE_JWT_SECRET, else GET /auth/v1/user)
  → tenant reads/writes via Lovable server API (VPS_TO_LOVABLE_API_TOKEN)
```

Optional VPS `/auth/signup` and `/auth/login` routes only proxy GoTrue. The frontend may call Supabase Auth directly.

| Table / store | Role |
|---|---|
| `auth.users` | Email/password, sessions (Supabase Auth) |
| `profiles` | Application profile (`id` = auth user id) |
| `slots` | Ownership (`user_id`) and persistent radio fields |
| `messages` | Tenant SMS inbox / outbox |
| `esim_uploads` | QR / storage key per slot |
| `esim-records` | Private Storage bucket |

See [SQLITE_INVENTORY.md](SQLITE_INVENTORY.md) for operational SQLite that remains.
See [VPS_LOVABLE_SERVER_API.md](VPS_LOVABLE_SERVER_API.md) for the VPS → Lovable contract.

Do **not** put `FARM_SERVICE_TOKEN`, `FARM_AGENT_API_TOKEN`, `VOIDFIX_WEBHOOK_SECRET`, or `VPS_TO_LOVABLE_API_TOKEN` in browser JavaScript.

## Credential classes

| Class | Used for | Not used for |
|---|---|---|
| Supabase access token | User dashboard | Farm Agent, VoidFix webhook |
| `FARM_SERVICE_TOKEN` | Lovable **server-side** → VPS farm/SMS ops | User login / browser / Lovable tenant API |
| `VPS_TO_LOVABLE_API_TOKEN` | VPS → Lovable server tenant API | Browser, Farm Agent, inbound webhook |
| `FARM_AGENT_API_TOKEN` | VPS → Farm Agent | Users or browsers |
| `VOIDFIX_WEBHOOK_SECRET` | VoidFix inbound only | `/auth/*` or `/slots/*` |

`SUPABASE_SERVICE_ROLE_KEY` is **not required** and is unused on the VPS production path.

## Environment

Required for user token validation: `SUPABASE_URL`, `SUPABASE_ANON_KEY`.

Required for tenant reads/writes: `LOVABLE_API_URL`, `VPS_TO_LOVABLE_API_TOKEN` (minimum 32 characters; generate 32+ random bytes). Configure the token on the VPS production `.env` manually. Do not copy a local `.env` to production.

Optional local JWT verify: `SUPABASE_JWT_SECRET` (project JWT secret, never the anon key, never logged).

Optional cookie store for the **same** Supabase access token: `VPS_AUTH_COOKIE_NAME`.

Auth route flood control: `VPS_AUTH_RATE_*`. CORS: `VPS_ALLOWED_ORIGINS`.

Obsolete and unused: `VPS_AUTH_JWT_SECRET`, `VPS_AUTH_DB_PATH`, `VPS_AUTH_LOCK_*`, `SUPABASE_SERVICE_ROLE_KEY`.

## Ownership

Identity comes from the validated token `sub`. Browser `user_id` is ignored.

Farm assign claims authoritative `slots.user_id` through Lovable **before** creating a VPS job. User eSIM / SMS / actions return **404** when the caller does not own the slot.

Slot path IDs may be the deterministic farm UUID or the Lovable/Supabase `slots.id`.
