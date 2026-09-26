# VPS user authentication

The VPS (`https://api.loanerphones.com`) is the authoritative application auth service.

```
Browser / Lovable frontend
  → POST /auth/signup or /auth/login
  → VPS users/sessions SQLite
  → signed USER_ACCESS_TOKEN
  → Authorization: Bearer <USER_ACCESS_TOKEN> on user APIs
```

Do **not** send passwords to Lovable Cloud Auth for this product flow.
Do **not** put `FARM_SERVICE_TOKEN`, `FARM_AGENT_API_TOKEN`, or `VOIDFIX_WEBHOOK_SECRET` in browser JavaScript.

## Credential classes

| Class | Used for | Not used for |
|---|---|---|
| `USER_ACCESS_TOKEN` | User dashboard: `/auth/me`, `/slots`, SMS, actions, eSIM | Farm Agent, VoidFix webhook |
| `FARM_SERVICE_TOKEN` | Lovable **server-side** → VPS farm/SMS ops | User login / browser |
| `FARM_AGENT_API_TOKEN` | VPS → Farm Agent | Users or browsers |
| `VOIDFIX_WEBHOOK_SECRET` | VoidFix inbound webhook only | Any `/auth/*` or `/slots/*` |

## Frontend

Base URL: `https://api.loanerphones.com`

- `POST /auth/signup` `{email, password}`
- `POST /auth/login` `{email, password}`
- `GET /auth/me`
- `GET /auth/session`
- `POST /auth/logout`

Bearer tokens in `localStorage` are XSS-readable. Prefer `VPS_AUTH_COOKIE_NAME` (HttpOnly, Secure, SameSite=Lax) when the frontend origin is allowlisted in `VPS_ALLOWED_ORIGINS`. Tokens expire (`VPS_AUTH_ACCESS_TOKEN_TTL_SECONDS`, default 3600) and logout revokes the server session.

CORS never uses `*`. Localhost / 127.0.0.1 are allowed for development. Production origins go in `VPS_ALLOWED_ORIGINS` (comma-separated). No secrets belong in CORS config.

## Password reset and email verification

`POST /auth/forgot-password`, `/auth/reset-password`, `/auth/verify-email`, `/auth/resend-verification` persist hashed, single-use, expiring tokens.

**Email delivery is not implemented.** There is no SMTP/provider plugin in this repo. Do not treat forgot-password as having emailed the user.

Production integration point: `AuthService(..., token_sink=your_mailer)` in `tools/vps_backend_server.py` after `create_user` / `forgot_password` / `resend_verification`. The sink receives `("verify"|"reset", raw_token)` and must send mail off-process. Never log the raw token or the password.

## Environment (set by hand on the VPS)

Copy names from `.env.example`. Do not overwrite production `.env`.

Required for signup/login: `VPS_AUTH_JWT_SECRET` of at least 32 UTF-8 bytes (generate with `python tools/generate_vps_auth_secret.py --out /secure/path`, then paste into `.env`). Missing or shorter secrets disable user auth (`503 auth_not_configured`); the secret is never logged.

Optional: `VPS_AUTH_ACCESS_TOKEN_TTL_SECONDS`, `VPS_AUTH_COOKIE_NAME`, `VPS_AUTH_COOKIE_SECURE`, `VPS_ALLOWED_ORIGINS`, `VPS_AUTH_DB_PATH`, lock/rate-limit knobs, `VPS_ESIM_ALLOWED_URL_PREFIXES`.

## Database

Dedicated SQLite `VPS_AUTH_DB_PATH` (default `logs/vps_auth.sqlite`). Schema is created with `CREATE TABLE IF NOT EXISTS` (idempotent). It is **not** the outbound SMS job database. Do not delete `*.sqlite3` / `*.sqlite` on deploy.

## Ownership

Identity comes from the validated user token (`sub` + session `jti`). Browser `user_id` is ignored.

`FARM_SERVICE_TOKEN` assignment (with `user_id`) creates `slot_ownership`. A user JWT may call `POST /slots/{slot_id}/esim` only after that row exists for the authenticated user. Unowned deterministic slot UUIDs return **404**. User-owned slot/SMS/action routes also return **404** when the resource is not owned by that user.

## Safe deploy

Clone the commit into `/tmp`, compile, run tests, copy application files only, keep production `.env`, `slot_msisdn_map.json`, `voidfix_devices.json`, and SQLite files, restart systemd, then check `/health`, `/farm/status` (20/20), and `/auth/signup` against a throwaway account.
