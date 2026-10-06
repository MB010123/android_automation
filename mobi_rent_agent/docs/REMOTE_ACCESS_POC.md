# Remote-access POC — Bay 1 / Slot 1 / Pixel 6 only

Status: Slot-1 farm path live-tested. Customer HTTPS reverse proxy and VPS `.env` are
operator-owned. Feature flag is OFF by default.

## Production notes (after the Slot-1 POC)

Customer-facing `activation_state` on GET/POST remote-access:

| Value | Meaning |
| --- | --- |
| `REMOTE_ACCESS_READY` | Lease is active; QR not prepared yet |
| `DEVICE_REBOOTING` | QR push and/or reboot in progress (`placing_qr` / `rebooting` / waits) |
| `CUSTOMER_ACTIVATION_REQUIRED` | QR is on the Pixel; customer must finish Android SIM UI |
| `ACTIVATING` | Read-only observation saw partial evidence (not success) |
| `ACTIVE` | Four-layer observation returned `ACTIVATION_CONFIRMED` only |
| `FAILED` | QR prepare/reboot pipeline failed |

`ACTIVE` is never set merely because the QR file was uploaded. Incomplete or failed
telephony snapshots stay `CUSTOMER_ACTIVATION_REQUIRED` (`activation_observed=missing`).
`ACTIVE` requires Farm Agent `verdict=ACTIVATION_CONFIRMED` persisted as
`activation_observed=confirmed`.

### Who does what

| Actor | Responsibility |
| --- | --- |
| Customer browser | Supabase user JWT; never Farm/GADS admin tokens or ADB serials |
| Lovable / Supabase | Auth, rental/slot row, QR **upload and storage**; `POST /esim-uploads` is Lovable (not this repo) |
| VPS | Ownership chain, QR allowlist, GADS lease, prepare/reboot orchestration, observation persist |
| Farm Agent | HTTPS QR download, `adb push` + media scan, reboot, read-only dumpsys observation |
| GADS | Remote screen/input and temporary user only |
| Android | Customer Settings → SIMs → Add eSIM (manual) |

### GET Refresh vs Check activation

Both may persist observation. GET skips Farm while `prepare_state` is in progress and
reuses the last observation for `observe_cooldown` (20s) so Refresh polling does not
hammer ADB. `POST …/activation-status` always observes unless reboot is in progress.

### assign is not QR placement

Lovable onboarding currently calls Farm **assign** after the QR upload. Assign
downloads the image, tries to decode an LPA code, then runs the subscription
provisioner. On Bay 1 that provisioner correctly **refuses** unattended
`provision_esim` because the Pixel has no `WRITE_EMBEDDED_SUBSCRIPTIONS`, carrier
privileges, or Device Owner. That refusal is **not** a dead device. The VPS must
surface it as `provisioning_phase=requires_manual_action`.

QR files reach `/sdcard/DCIM/Camera/` only via
`POST /rentals/{rental_id}/remote-access/prepare-esim` → Farm
`remote_access_place_qr`. Assign never `adb push`es the gallery image.

### assign then 403 on activation-status

`POST /farm/slots/{bay}/assign` only starts provisioning. It does **not** create a GADS
session. `POST …/activation-status` requires an **active remote-access session**.
Calling it after assign but before `POST …/remote-access` is **403 forbidden**.
That is correct. Do not weaken auth. Create remote access first, then Check/Refresh.

The rental row must expose an **allowlisted public HTTPS** QR URL (`qr_code_url` /
`esim_qr_url` / …). A storage key without a fetchable HTTPS URL fails closed (`503
esim_ref_unavailable`), same as silent assign.

Rental end: Lovable/farm-service should still `POST …/remote-access/release`. The VPS
also sweeps stale GADS leases on farm heartbeat when the tenant row is gone, the
owner changed, or `ends_at` is past.

`REMOTE_ACCESS_PUBLIC_URL` must be HTTPS for customers. The hub itself should stay on
Tailscale (`REMOTE_ACCESS_PLATFORM_URL`). GADS stores passwords in MongoDB plaintext;
per-rental users are deleted on revoke/release.

## What this is (and is not)

A customer rents Slot 1, opens the Mobi-Rent portal, and receives a *temporary*
browser session on the assigned Pixel 6 (screen streaming + touch/keyboard) through a
self-hosted remote-access platform (GADS). The customer then completes the **normal
Android eSIM UI** themselves (Settings → Network & internet → SIMs → Add eSIM → scan
the QR the Farm Agent placed in the camera roll, or enter the code manually).

This POC does **not**:

- install eSIM profiles silently, call `EuiccManager`, or send `provision_esim`;
- use or request `WRITE_EMBEDDED_SUBSCRIPTIONS`, carrier privileges, Device Owner,
  factory reset, or any privileged Android image change;
- change behavior for any slot other than those in `REMOTE_ACCESS_POC_SLOT_IDS`
  (default `1`);
- replace the Mobi-Rent backend for rentals, customers, slot ownership, QR upload,
  billing, or auth. The platform only provides physical access.

GADS does **not** solve Android eSIM authorization. It only lets a human do the
UI steps remotely.

## Platform decision

`SELECTED_PLATFORM: GADS` (github.com/shamanec/GADS).

Why GADS over DeviceFarmer/STF for this farm:

- Android 15/16 handling exists in the provider (`provider/devices/android.go`:
  MediaProjection-based GADS-Android-stream, `PROJECT_MEDIA` appop, keyguard handling,
  ADB TCP enable). STF documents Android ≤15 and relies on minicap, which is fragile on
  15 and undocumented on 16 (the Pixel 6 in Bay 1 runs Android 16).
- Remote control does not require Appium.
- Workspaces (device belongs to exactly one) + API device lock/lease with TTL
  (`POST /devices/control/{udid}/lock?ttl_minutes=`, max 360) + admin `release`
  map directly onto rental start / rental end / return-to-pool.
- Simple Bearer REST (`POST /authenticate` → JWT; `POST /admin/user`;
  `DELETE /admin/user/{name}`; `GET /admin/devices`; SSE `GET /available-devices`).
- Provider runs on Windows, which matches the farm PC.

Important limitations (verified in source/docs, must be accepted for the POC):

- GADS stores user passwords in plaintext in MongoDB → per-rental users must be
  throwaway and deleted at rental end (the adapter does this).
- On Android 15+ the provider runs `locksettings set-disabled true` (keyguard off) and
  enables ADB over TCP on the device so MediaProjection stays alive. Slot 1 only.
- Android 15 QPR1+ shows a "screen being shared" chip; projection stops if the screen
  locks.
- `hub-ui` is proprietary/obfuscated; the Go core is AGPL-3.0.
- Devices are registered manually in the GADS Admin panel; provider restart after
  device config changes.
- hub-ui login is username/password only; there is no verified token-in-URL login.
  The backend therefore returns a one-time, per-rental, non-admin username/password
  (shown once at grant time, never stored).
- A GADS JWT is bound to the request Origin; the backend authenticates with no Origin
  header and uses the token server-side only.

## Architecture

```
Customer browser ── Mobi-Rent portal ── VPS backend (/rentals/{id}/remote-access*)
                                          │  authorizes: customer → rental → slot → device
                                          ├── GADS hub REST (admin creds server-side only)
                                          └── Farm Agent (/agent/tasks/run: reboot, remote_access_place_qr)
Farm PC:  GADS provider ── adb ── Pixel 6 (Slot 1, udid = slot_map.json["1"])
```

Isolation chain enforced by `application/remote_access_service.py::_authorize`:

1. `REMOTE_ACCESS_POC_ENABLED` must be true, else 403.
2. Authenticated Supabase user id (`customer_id`) is taken from the token, never from
   the body. Browser-supplied `device_id`/`slot_id`/`serial` in the body are ignored.
3. Tenant row for `rental_id` (Lovable "slot by rental") must exist and
   `row.user_id == customer_id`.
4. Bay-keyed ownership (`owner_of_slot(bay)`) must also name this customer.
5. `bay` must be in `REMOTE_ACCESS_POC_SLOT_IDS` and mapped in `slot_map.json`.
6. Rental end (`ends_at`/`end_at`/`rental_end`/`expires_at`/…) must not be past.
7. For device routes, an *active* session for this rental/customer/slot/device must
   exist.

Any failed link → `403 {"ok":false,"error":"forbidden"}` (uniform, no enumeration).

## Files

New:

- `domain/remote_access.py` — errors, `RemoteDeviceStatus`, `PlatformAccessGrant`,
  `RemoteAccessPlatform` protocol.
- `infrastructure/gads_remote_access.py` — `GadsHubClient`, `GadsRemoteAccessPlatform`,
  `gads_platform_from_config`.
- `infrastructure/remote_access_store.py` — SQLite session store (no passwords, no
  serials persisted).
- `application/remote_access_service.py` — adapter interface `create_remote_access`,
  `get_remote_access`, `revoke_remote_access`, `release_device`, `get_device_status`,
  `reboot_device`, plus customer wrappers and `prepare_esim`.
- `application/remote_access_farm_task.py` — farm tasks `remote_access_place_qr`
  (download validated QR → `adb push` to `/sdcard/DCIM/Camera/` → media scan) and
  `remote_access_activation_status` (read-only four-layer observation).
- `tests/test_remote_access_poc.py` — tests A–K.
- `docs/REMOTE_ACCESS_POC.md` — this file.

Modified:

- `infrastructure/config.py` — `REMOTE_ACCESS_*` settings on `AgentConfig`.
- `application/farm_task_types.py` — adds `remote_access_place_qr` and
  `remote_access_activation_status`.
- `application/farm_task_executor.py` — dispatches those farm tasks.
- `application/vps_lovable_routes.py` — `/rentals/{rental_id}/remote-access[...]`.
- `application/vps_api_contract.py` — new error codes.
- `tools/vps_backend_server.py` — wires the service (only when the flag is on).
- `infrastructure/vps_openapi_spec.py` — documents the routes.
- `.env.example` — placeholders.

## HTTP surface (customer, Supabase bearer token)

| Method | Path | Result |
| --- | --- | --- |
| POST | `/rentals/{rental_id}/remote-access` | 201 in-app session (`session_mode=in_app`, `stream_path`). No GADS hub-ui login. |
| GET | `/rentals/{rental_id}/remote-access` | 200 session + `activation_state` / `setup_phase`. Never returns platform credentials. |
| GET | `/rentals/{rental_id}/remote-access/stream` | MJPEG proxy of the assigned Pixel only |
| POST | `/rentals/{rental_id}/remote-access/control` | tap / swipe / type / back (server allowlist) |
| POST | `/rentals/{rental_id}/remote-access/complete` | 200 `phone_ready` only after confirmed eSIM + VoidFix verify; revokes session |
| POST | `/rentals/{rental_id}/remote-access/revoke` | 200; deletes platform user, releases device lease |
| POST | `/rentals/{rental_id}/remote-access/device-status` | 200 `{state, online, available, busy, adb_online}` |
| POST | `/rentals/{rental_id}/remote-access/reboot` | 202; async reboot + wait, progress in `prepare_state` |
| POST | `/rentals/{rental_id}/remote-access/prepare-esim` | 202; QR → DCIM → reboot → wait ADB → wait platform |
| POST | `/rentals/{rental_id}/remote-access/activation-status` | 200; read-only observation (`ACTIVE` only on `ACTIVATION_CONFIRMED`) |
| POST | `/rentals/{rental_id}/remote-access/release` | customer or farm-service; revoke + return slot to pool |

No route accepts a device id. Farm-service callers may only `release`. Customer request bodies are ignored.

## Environment variables

Set on the **VPS** (`tools/vps_backend_server.py`) and the **farm PC**
(`tools/farm_agent_status_server.py`; only the first two matter there):

```
REMOTE_ACCESS_POC_ENABLED=true          # default false
REMOTE_ACCESS_POC_SLOT_IDS=1            # default 1; do not widen for the POC
REMOTE_ACCESS_PLATFORM_URL=http://<gads-hub>:10000
REMOTE_ACCESS_PUBLIC_URL=https://remote.<your-domain>
REMOTE_ACCESS_ADMIN_USERNAME=<dedicated backend admin>
REMOTE_ACCESS_ADMIN_PASSWORD=<secret; VPS .env only>
REMOTE_ACCESS_WORKSPACE_ID=<workspace containing only the Slot 1 device>
REMOTE_ACCESS_SESSION_TTL_MINUTES=60    # 5..360
REMOTE_ACCESS_REBOOT_TIMEOUT_SECONDS=180
```

The VPS also needs `SLOT_MAP_PATH` (or `mobi_rent_agent/slot_map.json`) containing the
Slot 1 entry so it can map slot → udid. Only allowlisted entries are loaded.

## Services required

- MongoDB (GADS dependency) — e.g. `docker run -d --name gads-mongo -p 27017:27017 mongo:6`.
- GADS hub (Go binary or Docker) reachable from the VPS; front it with TLS for
  `REMOTE_ACCESS_PUBLIC_URL`.
- GADS provider on the farm PC (Windows) with `adb` on PATH. Appium is not required
  for remote control.
- Existing Farm Agent (`8790`) and VPS backend (`8080`) — unchanged unless the flag is on.

## Slot 1 mapping and device registration

Exact mapping: `slot_map.json["1"]` → `18171FDF6005WG` (Bay 1, Pixel 6). The service
never hardcodes the serial; it reads the existing slot map filtered to
`REMOTE_ACCESS_POC_SLOT_IDS`.

Registration steps (manual, GADS Admin panel, no device changes yet):

1. Start MongoDB, then the hub. Create the initial admin, then a **second** admin
   account used only by the backend (`REMOTE_ACCESS_ADMIN_*`).
2. Admin → Workspaces → create `mobirent-poc-slot1`. Copy its id into
   `REMOTE_ACCESS_WORKSPACE_ID`.
3. Admin → Providers → add a provider for the farm PC (OS Windows, Android enabled).
4. Admin → Devices → add device: UDID `18171FDF6005WG`, OS Android, OS version 16,
   provider = farm PC, workspace = `mobirent-poc-slot1`. Do **not** add any other
   device.
5. On the farm PC, start the provider with the nickname configured in step 3. The
   provider installs GADS-Android-stream on the Pixel 6 and grants `PROJECT_MEDIA`.
   On Android 15+ it will also disable the keyguard and enable ADB TCP on that
   device (Slot 1 only). Confirm the device shows "live" in the hub UI and that the
   stream and taps work from a browser.

## Test procedure (unit, no live server)

```
cd mobi_rent_agent
python -m pytest tests/test_remote_access_poc.py -q
python -m pytest tests -q
```

## Live test procedure (Slot 1 / `18171FDF6005WG` only — run manually)

1. Fill `.env` on VPS and farm PC as above; restart both services. Confirm
   `GET /openapi.json` on the VPS now lists `/rentals/{rental_id}/remote-access`.
2. Confirm the device in the hub: backend-side sanity via a test customer's
   `POST …/remote-access/device-status` → `state: online`.
3. Customer A (owner of an active Slot 1 rental) → `POST …/remote-access` → 201 with
   `session_mode=in_app` and `stream_path`. Open the MJPEG via
   `GET {stream_path}` with the customer JWT (LoanerPhones UI). Do **not** open the
   GADS hub-ui. Confirm the Pixel screen is visible and allowed taps work.
4. Customer B with their own rental → `POST /rentals/{B}/remote-access` → 403.
   Customer B → `POST /rentals/{A}/remote-access` → 403.
5. Customer A → `POST …/remote-access/prepare-esim` → 202. Watch `GET …/remote-access`
   `prepare_state` go `placing_qr → rebooting → waiting_adb → waiting_platform → ready`.
   Farm log shows `qr_placed:/sdcard/DCIM/Camera/mobirent_esim_qr_….png`. Device
   reboots once (Slot 1 only).
6. Customer A, in the remote screen: Settings → Network & internet → SIMs → Add eSIM →
   scan QR from Photos (or enter the code) and complete the carrier flow manually.
7. Customer A → `POST …/remote-access/revoke` → 200; browser session is gone.
8. Farm-service → `POST …/remote-access/release` → 200; a new Slot 1 rental can be
   granted.

## Rollback

1. Set `REMOTE_ACCESS_POC_ENABLED=false` (or remove the `REMOTE_ACCESS_*` lines) on
   the VPS and farm PC; restart both. Routes disappear; the farm task returns 403.
2. Stop the GADS provider and hub; delete the Slot 1 device from the hub.
3. Undo device-side provider changes on `18171FDF6005WG` only:
   `adb -s 18171FDF6005WG shell locksettings set-disabled false`,
   `adb -s 18171FDF6005WG usb`, and uninstall `com.shamanec.stream` if desired.
4. Optionally delete `remote_access_sessions.sqlite` next to the VPS jobs DB.

Nothing in this POC modifies the companion app, Android security policy, or other slots.
