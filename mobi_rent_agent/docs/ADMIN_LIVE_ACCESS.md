# Admin live phone access (backend)

Administrators can view and control any PhoneFarmBox bay by its farm slot number
(1–20) without creating a rental. Lovable is unchanged: these routes are VPS-only.

## Endpoints

All four require `Authorization: Bearer <USER_ACCESS_TOKEN>` from a **verified
administrator**. Farm-service tokens and ordinary customer JWTs are rejected.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/admin/devices/{slot_id}/live-access` | Create or reuse a short-lived admin session |
| `GET` | `/admin/devices/{slot_id}/live-access/stream?session={session_id}` | Proxied MJPEG (same GADS mechanism as customer stream) |
| `POST` | `/admin/devices/{slot_id}/live-access/control` | Allowlisted tap / swipe / navigation |
| `POST` | `/admin/devices/{slot_id}/live-access/close` | End only this admin session |

`slot_id` is the farm bay (`1`–`20`), not the public slot UUID and not a rental id.

### Authentication

Administrator identity is decided **server-side** after the Supabase JWT is verified:

1. JWT `sub` is listed in `VPS_ADMIN_USER_IDS` (comma-separated UUIDs on the VPS), or
2. Signed JWT `app_metadata.mobi_rent_role` or `app_metadata.role` is `admin`.

Request-body `role`, `is_admin`, `user_metadata`, and the Supabase JWT `role`
claim (`authenticated`) are never authorization.

`GET /auth/me` returns `"role": "admin"` only for those verified identities.

### Session

POST returns:

```json
{
  "ok": true,
  "session_id": "<uuid>",
  "slot_id": 3,
  "status": "active",
  "active": true,
  "expires_at": 1760000000,
  "stream_path": "/admin/devices/3/live-access/stream?session=<uuid>",
  "allowed_controls": ["tap", "swipe", "type", "back", "home", "recents",
                       "notification_shade", "quick_settings", "rotate",
                       "settings", "airplane_mode"],
  "coordinate_space": "native_device_pixels",
  "session_mode": "admin_live_access",
  "rental_unchanged": true,
  "cleanup_unchanged": true
}
```

The browser never receives GADS admin credentials, hub URLs, ADB serials, or
workspace ids. Stream/control bind `session_id` to **that admin** and **that bay**.

Default TTL is 15 minutes (`ADMIN_LIVE_ACCESS_TTL_MINUTES`, 5–60).

## How this reaches GADS without a rental

The VPS already holds `REMOTE_ACCESS_ADMIN_*` and talks to the GADS hub. Customer
remote access calls `grant_access(rental_id)` to mint a per-rental GADS user.
Admin live access does **not** call `grant_access`, `revoke_access`, or
`release_device`. It streams and controls with the existing hub admin token
(server-side only). A customer lease on the same phone is left intact.

## Supported controls

Same semantic allowlist as customer in-app control: `tap`, `swipe`, `type`,
`back`, `home`, `recents`, `notification_shade`, `quick_settings`, `rotate`,
`settings`, `airplane_mode`.

Customer session restrictions (shade/QS/Camera) are **not** applied, so an
operator can reach Settings / VoidFix / Quick Settings during cleanup.

`airplane_mode` is accepted then returns **501** `action_not_supported` (no
approved Android 16 Farm/GADS/companion path). There is no dedicated GADS
Settings endpoint; `settings` is accepted and the operator opens Settings by tap.

Rejected: `shell`, `adb`, `keyevent`, package/intent fields, `slot_id` / `udid` /
`serial` in the control body, reboot, factory reset, device-owner changes.

Control body must include `session_id`. Coordinates are 0–8192 native pixels.

## Cleanup and reuse

Opening or closing live access does **not** mean cleanup was performed.

Authoritative reuse approval remains:

`POST /farm/slots/{bay}/cleanup-verified`  
`Authorization: Bearer <FARM_SERVICE_TOKEN>` only.

A cancelled/released phone stays out of `GET /farm/slots/available` and cannot
be assigned until that farm-service call clears the durable CLEANUP REQUIRED
hold. Admin live access cannot call it and does not fake success.

## Errors

| HTTP | `error` | When |
|------|---------|------|
| 400 | `invalid_slot` | Bay not 1–20 |
| 400 | `invalid_request` | Missing/invalid `session_id` |
| 401 | `unauthorized` | Missing/invalid user JWT (or farm-service token) |
| 403 | `admin_forbidden` | Authenticated but not an administrator |
| 403 | `live_access_slot_mismatch` | Session belongs to another bay |
| 403 | `session_expired` | TTL elapsed |
| 403 | `forbidden_control` | Disallowed action or identity field |
| 404 | `slot_not_found` | Bay has no slot-map entry |
| 404 | `live_access_not_found` | Unknown session |
| 409 | `phone_offline` | Device registered but offline |
| 422 | `invalid_control` | Bad coordinates / unknown action |
| 429 | `rate_limited` | Per-bay / global admin live-access limiter |
| 501 | `action_not_supported` | `airplane_mode` |
| 502 | `gads_unavailable` | Hub/control/stream failure |
| 503 | `phone_unavailable` | Unregistered, not GADS-enabled, or feature off |

## Configuration (VPS)

```
VPS_ADMIN_USER_IDS=<supabase-user-uuid>,<uuid>
ADMIN_LIVE_ACCESS_ENABLED=true
ADMIN_LIVE_ACCESS_TTL_MINUTES=15
```

`ADMIN_LIVE_ACCESS_ENABLED` is **required**. Customer `REMOTE_ACCESS_POC_ENABLED`
does **not** turn these routes on. Also requires the existing
`REMOTE_ACCESS_PLATFORM_URL` / `REMOTE_ACCESS_ADMIN_USERNAME` /
`REMOTE_ACCESS_ADMIN_PASSWORD` and `SLOT_MAP_PATH`. No new GADS user is created.
Empty `VPS_ADMIN_USER_IDS` plus no signed `app_metadata` admin claim means every
caller is a customer (`403 admin_forbidden`).

Redeploy the VPS backend after setting `VPS_ADMIN_USER_IDS`. Do not put those
UUIDs in the Lovable frontend. Do not restart Farm Agent or GADS for this API.

## Missing upstream capability

GADS/Farm still cannot toggle Airplane Mode on Pixel 6 Android 16. Admin live
access reports that honestly (501). There is no GADS “open Settings” RPC;
operators tap the Settings icon. Cleanup **approval** is still the existing
farm-service `cleanup-verified` route, not this live-view API.
