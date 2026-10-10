# Customer remote-phone API (frontend integration spec)

Status: backend contract for Lovable (external) plus the in-repo `web/` live-screen copy.
The production customer UI is **Lovable-only and is not in this repo**; wire Home/Recents/shade/QS/rotate from this contract. `mobi_rent_agent/web/` is the in-repo control surface used as a copy target.
Do **not** connect the browser to GADS, Farm Agent, ADB, or the Pixel.

### Lovable must send these control actions

`POST /rentals/{rental_id}/remote-access/control` with customer JWT. Exact bodies:

```json
{ "action": "tap", "x": 540, "y": 960 }
{ "action": "swipe", "start_x": 540, "start_y": 1600, "end_x": 540, "end_y": 800, "duration_ms": 300 }
{ "action": "type", "text": "Hello" }
{ "action": "back" }
{ "action": "home" }
{ "action": "recents" }
{ "action": "notification_shade" }
{ "action": "quick_settings" }
{ "action": "rotate", "orientation": "portrait" }
{ "action": "settings" }
```

`orientation` is `portrait` or `landscape`. Aliases: `overview` → `recents`, `notifications` → `notification_shade`, `android_settings` → `settings`. Never send keycodes, ADB, shell, serial, UDID, or workspace ids.

This repo's backend allows Home/Recents/shade/QS/rotate/tap/swipe/type/back from session start. **`setup_mode` is telemetry and must not hide buttons.** `{ "action": "settings" }` and opening the Android Settings icon/QS gear do **not** open the Settings homepage — they redirect once to Add eSIM (`android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS`). Back from that eSIM root goes Home. VoidFix and other admin surfaces are sent Home. This is a session-layer redirect, not the old setup-mode kiosk: leaving eSIM to Home does **not** auto-relaunch SIM Settings.

Control 200 bodies may include `restricted_destination` (`add_esim` or `home`) and `restriction` (`settings_redirected`, `esim_back_to_home`, `sensitive_app_blocked`). Successful QR upload may include `restricted_destination=add_esim` and `restriction=qr_navigated_to_add_esim`. The live stream stays up. Do not treat those fields as a disconnect.

Production default: `CUSTOMER_SESSION_RESTRICTIONS` is **ON when unset**. `0` / `false` / `no` / `off` disables Settings→Add eSIM, QR navigate-to-Add-eSIM, VoidFix/admin Home redirects, and Back-from-eSIM-root→Home. Flag off must not 502 `{ "action": "settings" }`.

Unexplained rejections (`invalid_control`, `forbidden_control`, `phone_not_ready`, `setup_state_blocked`) persist until **all three** are updated:

1. Lovable sends the semantic actions above (not only tap/swipe/type/back).
2. VPS is deployed with this contract (production VPS was not deployed with 66dfc91).
3. Farm Agent is restarted so `setup_session_input` kinds `home` / `recents` / `notification_shade` / `quick_settings` exist (needed when GADS has no matching nav endpoint). Settings → Add eSIM uses the existing inspect `recover` intent and does **not** need a Farm restart.

Architecture:

```
Customer frontend
  → VPS backend  (https://api.loanerphones.com)
    → Farm Agent / GADS
      → Pixel phone
```

Base URL: the public VPS API host (example: `https://api.loanerphones.com`).
Auth: Supabase **user** JWT only.

```
Authorization: Bearer <customer_access_token>
```

Never send Farm tokens, GADS credentials, ADB serials, workspace IDs, or
internal platform passwords from the browser.

`rental_id` is a UUID. The backend derives the assigned bay and device from
the rental row. Request bodies must not name `slot_id`, `device_id`, `udid`,
`serial`, or `workspace_id`.

---

## 1. Start session

`POST /rentals/{rental_id}/remote-access`

Empty JSON body `{}` is fine. The server ignores client-supplied device ids.

Creates a GADS session for this rental or **reuses** the existing one (HTTP 200).
`requires_manual_action` does **not** block this call. The backend does **not**
take a GADS exclusive device lock.

### Success (created) — 201

```json
{
  "ok": true,
  "rental_id": "00000000-0000-4000-8000-000000000099",
  "slot_id": 1,
  "status": "active",
  "active": true,
  "expires_at": 1730000000,
  "session_mode": "in_app",
  "stream_path": "/rentals/00000000-0000-4000-8000-000000000099/remote-access/stream",
  "allowed_controls": ["tap", "swipe", "type", "back", "home", "recents", "notification_shade", "quick_settings", "rotate", "settings"],
  "setup_mode": true,
  "coordinate_space": "native_device_pixels",
  "ui_state": "live_phone_screen",
  "setup_phase": "esim",
  "setup_complete": false,
  "activation_state": "REMOTE_ACCESS_READY",
  "qr_ready": false,
  "guidance": "Open the Pixel remotely. When you are ready, continue so we can place the eSIM QR on the phone."
}
```

### Success (reused) — 200

Same shape. `ok: true`, `active: true`. Do **not** treat 200 as an error.
Call this again on reconnect; do not expect a new lease.

Safe fields only. Responses never include ADB serial, GADS workspace id,
platform password, JWT, or hub URLs.

### Errors


| HTTP    | `error`              | When                                                       |
| ------- | -------------------- | ---------------------------------------------------------- |
| 401     | `unauthorized`       | Missing/invalid customer JWT                               |
| 404     | `rental_not_found`   | Unknown rental id                                          |
| 403     | `rental_not_owned`   | Signed-in user does not own the rental                     |
| 403     | `session_expired`    | Rental end time has passed                                 |
| 409     | `phone_offline`      | Assigned Pixel is offline                                  |
| 409     | `remote_access_busy` | **Another rental** currently holds the remote-access lease |
| 503     | `phone_unavailable`  | Bay not mapped / remote access disabled                    |
| 502/503 | `gads_unavailable`   | Screen service not configured or rejected the grant        |


`remote_access_busy` is **only** the cross-rental lease conflict. The customer's
own live stream is not "busy".

---

## 2. Connect stream

`GET /rentals/{rental_id}/remote-access/stream`

Must send the same `Authorization: Bearer` header. `<img src>` cannot set that
header — use `fetch` (or a worker) and render decoded JPEGs on a canvas.

Do not call GADS `android-stream-mjpeg` from the browser.

The VPS keeps the GADS session. Opening or reading the stream does **not**
revoke it and does **not** block `POST .../control`.

### Content type

```
Content-Type: multipart/x-mixed-replace; boundary=frame
Cache-Control: no-store
X-Accel-Buffering: no
```

If GADS supplies a different `boundary=` parameter, the VPS forwards it
unchanged. If GADS omits `boundary=`, the VPS uses `boundary=frame`.

### Frame format

Each part is a JPEG (`Content-Type: image/jpeg`) of the **current phone
framebuffer**. Typical part layout (CRLF line endings):

```
--frame
Content-Type: image/jpeg

<binary JPEG>
--frame
Content-Type: image/jpeg

<binary JPEG>
...
```

The delimiter is `--` + the boundary token from the `Content-Type` header
(example token: `frame` → delimiter `--frame`). A terminating `--frame--`
may appear on disconnect; treat it as end-of-stream.

### Authentication

Same customer JWT as start-session. Unauthenticated → 401. Wrong rental owner
→ 403 `rental_not_owned`. No active session → 409 `remote_access_not_ready`
or 403 `session_expired`.

### Connection lifecycle

1. `POST .../remote-access` until `active: true`.
2. `GET .../stream` and keep the HTTP connection open.
3. Parse multipart parts; each JPEG is one frame.
4. Control requests use a **separate** HTTP connection.
5. On tab hide, pause rendering if you want; keep the stream or reconnect later.
6. `POST .../complete` (or rental cancel) closes the session; the stream then ends.

The backend does **not** recreate or revoke the GADS session per frame.

### Reconnect

If the stream socket drops (`body` ends, 502 `gads_unavailable`, network error):

1. Abort the previous reader.
2. `POST /rentals/{rental_id}/remote-access` again (expect 200 reuse).
3. Open `GET .../stream` again.
4. Backoff ~1s between attempts. Do not tight-loop.

Do not treat reconnect as a new rental.

---

## 3. Decode / render frames

Recommended:

1. Read the multipart stream with `fetch` + `ReadableStream`.
2. Split on the boundary delimiter.
3. For each part, take bytes after the part headers (`image/jpeg`).
4. `createImageBitmap` / `Image.decode` the JPEG.
5. Draw to a `<canvas>` sized to the **display box** (CSS pixels). Keep the
  bitmap's `width`/`height` as native frame dimensions.

Do **not** assign each JPEG to `img.src` in a way that races with click
coordinates. Overlay hit-testing must use the same rectangle as the drawn
frame (object-fit contain letterboxing included).

---

## 4. Native phone resolution

Coordinates are **native device pixels**, origin top-left, x right, y down.

Resolution sources, in order:

1. `GET .../device-status` field `native_resolution: { width, height }` from Farm
  Agent `adb shell wm size` **Physical size** (not the MJPEG frame, which may be 720px).
2. If that field is absent, `native_resolution_unavailable` explains why. Do not
  invent 1080×2400. You may fall back to JPEG `naturalWidth`/`naturalHeight` only
   as a last resort; that is the stream size, not native coordinates.
3. Session JSON `coordinate_space` is always `"native_device_pixels"`.

Do not assume a hardcoded Pixel size. Frame size can match the panel
(commonly 1080×2400 class) but the contract is "whatever the stream frame is".

`native_resolution` is omitted when unknown. First decoded frame is enough
to start mapping taps.

---

## 5. Map browser coordinates → native phone coordinates

Let `rect` be `canvas.getBoundingClientRect()` of the **drawn phone image**
(not the full page). If you letterbox, use the inner image rectangle, not the
empty bars.

```
native_x = (client_x - rect.left) / rect.width  * native_width
native_y = (client_y - rect.top)  / rect.height * native_height
```

Round to integers. Clamp to `[0, native_width-1]` / `[0, native_height-1]`.

Send those integers in tap/swipe bodies. The backend rejects coordinates
outside `0..8192`. Bottom-edge navigation gestures and status-bar pull-downs
are allowed from session start (before and after eSIM activation). Prefer
semantic Home / Recents / `notification_shade` / `quick_settings` rather than
only coordinate swipes.

---

## 6. Tap

`POST /rentals/{rental_id}/remote-access/control`

```json
{ "action": "tap", "x": 540, "y": 960 }
```

### 200

```json
{ "ok": true, "action": "tap", "forwarded": true }
```

When Settings or a blocked admin surface was redirected, the same 200 also
includes `"restricted_destination": "add_esim"|"home"` and a `restriction`
code. Ordinary taps omit those fields. The stream stays up.

---

## 7. Swipe

```json
{
  "action": "swipe",
  "start_x": 540,
  "start_y": 1600,
  "end_x": 540,
  "end_y": 800,
  "duration_ms": 300
}
```

Aliases also accepted: `x`/`y`/`x2`/`y2` instead of `start_*`/`end_*`,
and `duration` instead of `duration_ms`. Duration is optional (1–5000 ms).

Bottom-edge upward swipes are accepted as normal Android navigation (Home /
Recents / Back gestures). Status-bar pull-downs are forwarded as normal shade
gestures from session start. Prefer semantic `{ "action": "home" }`,
`{ "action": "recents" }`, `{ "action": "notification_shade" }`,
`{ "action": "quick_settings" }` — never Android keycodes.

Assigned customers keep Home, Recents, shade, Quick Settings, rotate, apps,
and gestures before, during, and after eSIM activation. `setup_mode` is
telemetry (true until `ACTIVATION_CONFIRMED` and not VoidFix) and does **not**
gate that allowlist. Settings (icon, QS gear, or `{ "action": "settings" }`)
is redirected once to Add eSIM. Back from the eSIM root returns Home, not the
Settings homepage. Nested eSIM confirm dialogs still receive one Back.
Leaving eSIM by Home/Recents does **not** auto-relaunch SIM Settings.
VoidFix (`org.voidfix.smsgateway`) and the package installer are sent Home.
Unrelated Settings (security, accounts, developer, Wi-Fi/network, apps,
system) redirect once to Add eSIM. Delete eSIM on the same SIM-profiles
screen is not intercepted.
Enforcement uses GADS `home` or the existing Farm `am start -a
android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS`. It never restarts GADS,
never uses Device Owner / lock-task, and never deletes eSIMs.
QR upload / eSIM activation remain a customer action, not a kiosk. Reconnect
and page refresh reuse the session. The live stream stays available while the
session is active — do not hide it just because `ui_state=phone_ready` or
because a restriction was reported.

---

## 8. Type

```json
{ "action": "type", "text": "Hello" }
```

`text` is a non-empty string, max 64 characters, no control characters.

The phone must already have a focused text field. This is not an ADB keyevent.

---

## 9. Back

```json
{ "action": "back" }
```

No coordinates. This is Android Back only.

```json
{ "action": "home" }
```

```json
{ "action": "recents" }
```

```json
{ "action": "notification_shade" }
```

```json
{ "action": "quick_settings" }
```

```json
{ "action": "rotate", "orientation": "portrait" }
```

```json
{ "action": "settings" }
```

`orientation` is `portrait` or `landscape`. Recents also accepts alias `overview`.
Shade also accepts `notifications`. `settings` opens Add eSIM, not the Settings
homepage. No coordinates and no keycodes. The VPS maps these to GADS; Farm
Agent fallback uses the matching Android nav / `cmd statusbar` event internally
(not customer ADB). Rotate is GADS-only — if the hub has no rotation endpoint
the control returns 502 `gads_unavailable` (no `wm` overscan). Settings
redirect uses the existing Farm inspect `recover` intent
`android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS` (no Farm restart required).

Forbidden (do not send): `keycode`, `keyevent`, `adb`, `shell`, `command`,
serials, workspace ids.


| HTTP | `error`                                                                                          |
| ---- | ------------------------------------------------------------------------------------------------ |
| 422  | `invalid_control` (bad/missing numbers, unknown action, extra fields)                            |
| 403  | `forbidden_control` (ADB/identity spoof/raw keys)                                                |
| 409  | `remote_access_not_ready`                                                                        |
| 403  | `session_expired` / `rental_not_owned`                                                           |
| 502  | `gads_unavailable` (GADS rejected or missing; never a fake 200)                                  |
| 503  | `farm_unreachable` (Farm fallback needed and Farm is down)                                       |
| 504  | `timeout` (Farm fallback timed out)                                                              |


---

## 10. Handle errors

Every JSON error uses:

```json
{
  "ok": false,
  "error": "phone_offline",
  "message": "The assigned phone is offline"
}
```

Switch on `error`, not on the human `message`, and **not** on HTTP 409 alone.


| `error`                   | Meaning                                              | Frontend                                        |
| ------------------------- | ---------------------------------------------------- | ----------------------------------------------- |
| `unauthorized`            | JWT missing/invalid                                  | Re-auth                                         |
| `rental_not_found`        | Bad rental id                                        | Leave screen                                    |
| `rental_not_owned`        | Wrong user                                           | Leave screen                                    |
| `phone_offline`           | Pixel offline                                        | Retry later; show offline                       |
| `phone_unavailable`       | Bay/device not ready                                 | Support / wait                                  |
| `remote_access_busy`      | Another rental owns the lease                        | Do not retry as if "phone busy from our stream" |
| `remote_access_not_ready` | No active session                                    | Call start-session                              |
| `session_expired`         | Rental or session ended                              | End UI                                          |
| `gads_unavailable`        | Screen service down                                  | Retry with backoff                              |
| `invalid_control`         | Bad tap/swipe/type body                              | Fix mapping                                     |
| `forbidden_control`       | ADB/keys/identity spoof                              | Do not retry as a gesture                       |
| `manual_esim_required`    | Informational code; **does not** block remote access | Show "finish eSIM in Settings"                  |
| `setup_incomplete`        | Legacy setup-finish warning                          | Do not treat as stream failure                  |


Never display a generic "The phone is busy" for every 409.

---

## 11. Handle reconnect

```
onStreamError:
  wait 1s
  POST /rentals/{id}/remote-access     // reuse
  GET  /rentals/{id}/remote-access/stream
```

If start-session returns `phone_offline` or `gads_unavailable`, keep the last
frame, show a reconnecting state, retry. If `session_expired` / `rental_not_owned`,
stop.

Stream and control are independent HTTP calls. A tap during streaming is
expected. Duplicate start-session calls are safe (200 reuse, one GADS user).

---

## 12. Complete session

`POST /rentals/{rental_id}/remote-access/complete`

Closes this customer's remote session and runs **safe QR-file cleanup** only.
It does **not** delete eSIM, factory-reset, or change phone security state.

eSIM confirmation is **not** required to hang up the remote screen.

### 200

```json
{
  "ok": true,
  "setup_complete": false,
  "ui_state": "session_closed",
  "remote_session": "closed",
  "activation_observed": null,
  "voidfix_observed": null,
  "esim_deleted": false,
  "factory_reset": false
}
```

`ui_state=phone_ready` / `setup_complete=true` only after the existing
read-only Farm observer returns `verdict=ACTIVATION_CONFIRMED` (persisted as
`activation_observed=confirmed`, `activation_state=ACTIVE`). QR upload, a live
GADS session, phone-online, or `requires_manual_action=false` is not Phone Ready.
Hanging up without confirmation still returns `ui_state=session_closed`.
Either way the remote session is closed without deleting the eSIM.

Also acceptable: `POST .../remote-access/revoke` (session only) and
`POST /rentals/{rental_id}/cancel` (rental lifecycle + phone-service release).

---

## Device status

`GET /rentals/{rental_id}/remote-access/device-status`  
(`POST` with `{}` is also accepted.)

Does **not** require an active remote session. Does **not** report the
caller's own stream as busy.

```json
{
  "ok": true,
  "state": "online",
  "online": true,
  "adb_online": true,
  "remote_access_available": true,
  "remote_access_busy": false,
  "requires_manual_action": true,
  "session_active": true,
  "busy": false,
  "available": true,
  "slot_id": 1,
  "coordinate_space": "native_device_pixels",
  "native_resolution": { "width": 1440, "height": 3120 },
  "imei2": "353456789012345",
  "imei2_status": "known",
  "eid": null,
  "eid_status": "unknown",
  "carrier": "T-Mobile",
  "carrier_status": "known",
  "phone_number": null,
  "phone_number_status": "unknown",
  "cellular_status": "unknown"
}
```


| Field                     | Meaning                                                                                      |
| ------------------------- | -------------------------------------------------------------------------------------------- |
| `state`                   | `online` | `offline` | `unavailable`                                                         |
| `requires_manual_action`  | Customer must finish eSIM in Android Settings/LPA. **Remote access remains allowed.**        |
| `remote_access_available` | Phone online and no other rental holds the lease                                             |
| `remote_access_busy`      | Another rental owns the lease                                                                |
| `native_resolution`       | Optional; otherwise use JPEG frame size                                                      |
| `imei2`                   | Assigned phone IMEI2 from `public.slots.imei2`; otherwise `null`                             |
| `imei2_status`            | `known` | `unknown`. Never a placeholder                                                     |
| `eid`                     | Assigned phone eUICC EID when already stored on the tenant slot/rental row; otherwise `null` |
| `eid_status`              | `known` | `unknown`. Never a placeholder                                                     |
| `carrier`                 | Assigned slot carrier from `public.slots.carrier_name`; otherwise `null`                     |
| `carrier_status`          | `known` | `unknown`. Never a placeholder                                                     |
| `phone_number`            | TEMPORARY Farm `slot_msisdn_map` number for the assigned slot; else `public.slots.phone_number`; otherwise `null` |
| `phone_number_status`     | `known` | `unknown`. Never a placeholder                                                     |
| `cellular_status`         | Always `unknown`. There is no approved live radio reader                                     |


These identity fields are **read-only inventory passthrough** for the owned
assigned rental only. Empty or whitespace inventory is `null` / `unknown`.
Values are never inferred from Wi-Fi, ADB online, or GADS session state.
IMEI1 is not returned (US Mobile uses IMEI2).

**TEMPORARY `phone_number` source:** Farm VoidFix/SMS routing numbers from
`slot_msisdn_map.json` (`load_slot_msisdn_map` / `SLOT_MSISDN_MAP_PATH`) for
the authorized rental's assigned slot only. The customer cannot choose a
slot, serial, or workspace. **Precedence:** Farm map for that assigned slot
if present and non-empty; otherwise `public.slots.phone_number`. If both
exist and differ, the Farm map wins. Never fabricate. Production VPS must
have the map file (or `SLOT_MSISDN_MAP_PATH`) to show numbers; do not
commit the live map JSON.

EID is **not** read live from the Pixel. There is no allowlisted Farm Agent
task, companion identity field, `SLOT_SAFE_FIELDS` column, or
`device_registry` field that collects it. Inventing `adb shell`
`dumpsys` / `service call` / `getprop` for the customer API is forbidden, so
missing EID inventory is reported honestly as `eid=null` / `eid_status=unknown`.
If Lovable later stores `eid` on the owned slot row from a prior approved
read, this endpoint returns that value for the assigned rental only.
Live Pixel IMEI is also not collected here: IMEI2 is returned only when
already stored on `public.slots.imei2`.

---

## QR upload (manual eSIM)

Automatic eSIM provisioning is **disabled**. The backend never installs an
eSIM, never calls EuiccManager, and never retries silent provisioning.

`POST /rentals/{rental_id}/esim/upload`

- `multipart/form-data`
- field name: `qr_image`
- PNG / JPEG / WEBP bytes, max 5 MiB
- Places the file in the phone Camera directory
- Does not require a GADS session

Workflow:

1. Upload QR. When `CUSTOMER_SESSION_RESTRICTIONS` is on (default), the
   assigned rental bay is one-shot navigated to Add eSIM
   (`MANAGE_ALL_SIM_PROFILES_SETTINGS`) using the Farm inspect `recover`
   intent. This uses the rental→slot map only. It does not start or restart
   GADS, does not expose serials, and is skipped when the flag is off.
   Placement still succeeds if that navigate fails.
2. Start remote access (allowed even when `requires_manual_action` is true).
3. If the customer opens Settings (icon / QS gear / `{ "action": "settings" }`),
   the session layer sends them to Add eSIM again (one-shot, not a loop).
   They scan the gallery QR. They should not land on the Settings homepage.
4. Backend observes activation (`POST .../activation-status`).
5. `activation_state` becomes `ACTIVE` only after confirmed observation.

---

## Troubleshoot Phone

`POST /rentals/{rental_id}/remote-access/troubleshoot`

Customer JWT. Assigned rental phone only. Body is `{}` or `{ "action": "diagnose" }`
for a read-only snapshot, or `{ "action": "reboot" }` for the existing Farm reboot
task. No slot, serial, ADB, or shell fields.

Diagnostics use existing device-status and stored activation/setup state.
`cellular_status` is `unknown` (Farm health does not expose radio). Reboot does
not wait for GADS to return and does not change eSIM, VoidFix, or setup-mode.


| HTTP | Meaning                                              |
| ---- | ---------------------------------------------------- |
| 200  | diagnostics snapshot                                 |
| 202  | `recovery=reboot_requested` (Farm reboot accepted)   |
| 429  | cooldown                                             |
| 409  | another troubleshoot/reconnect in flight on this bay |
| 503  | Farm unreachable (reboot)                            |
| 504  | Farm timeout (reboot)                                |


---

## Reconnect Cellular

`POST /rentals/{rental_id}/remote-access/reconnect-cellular`

Customer JWT. Targets only the rental's assigned bay. Empty JSON body.
Does not accept slot, serial, ADB, or shell fields.

The backend calls the existing Farm `airplane_cycle` task. Farm Agent currently
returns `501 action_not_supported` because there is no approved airplane-mode
command on `mobi_rent.network` (VPN status/start/stop only) and raw
`adb shell settings` is not exposed. This endpoint does **not** fake success.


| HTTP | `error`                                      |
| ---- | -------------------------------------------- |
| 501  | `action_not_supported`                       |
| 429  | `rate_limited` (cooldown)                    |
| 409  | `remote_access_busy` (in-flight on this bay) |
| 504  | `timeout`                                    |
| 403  | `rental_not_owned` / `forbidden_control`     |
| 404  | `rental_not_found`                           |


---

## Customer session Settings restrictions

Applies only while an authorized customer remote-access session is active.
Admin / Farm / operator VoidFix paths are unchanged.

**Inspected on Pixel 6 Slot 11 (read-only `setup_session_inspect`, recover=false):**
Android 16 / SDK 36, unlocked Nexus Launcher, no live Settings tap performed.
VoidFix package from the SMS-role dump: `org.voidfix.smsgateway`.
Add eSIM entry point already allowlisted on Farm:
`am start -a android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS`
(same intent the old recover path used). Permitted eSIM screens include
`MobileNetworkActivity` and `com.google.android.euicc` / provision UI.

**Implemented (session layer, GADS stays up, bays 1–20 via rental→slot):**
- Settings icon / QS gear / `{ "action": "settings" }` → Add eSIM once
- Unrelated Settings (security, accounts, developer, Wi-Fi/network, apps, system) → Add eSIM
- Back from eSIM root → Home (not Settings homepage)
- One Back still reaches nested eSIM confirm/cancel
- VoidFix and package installer → Home
- QR upload → one-shot Add eSIM navigate on the authorized rental bay
- `restricted_destination` on the control/QR 200; stream is not blanked
- Cancel / complete still revoke the session and do **not** delete eSIMs
- `inspect recover=true` is used only for that Add eSIM one-shot, never a loop

**Known Android / Farm limitation:**
Farm `setup_session_inspect` recover is a no-op when the current page is already
classified as eSIM-allowed by `setup_activity_guard` (for example Network
dashboard: `networkdashboard` / some `telephony` surfaces). VPS still classifies
those as unrelated Settings and sends recover once. Closing the no-op needs a
Farm Agent change and restart; this VPS change does not restart Farm.

**Not implemented (would drop GADS / MediaProjection or needs new shell):**
- Device Owner / lock-task / hiding icons as enforcement
- Hard-blocking the delete/remove control on the same SIM-profiles screen
  (it is the Add eSIM surface; session layer cannot intercept that tap)
- Background recover loop when the customer is on Home or Chrome
- Customer-supplied package / component / ADB

**Rollback:** set `CUSTOMER_SESSION_RESTRICTIONS=0` on the VPS and reload that
process (unset remains ON). Do not factory-reset phones. Farm Agent
does not need a restart for the existing recover intent. Do not restart GADS.

---

## Cancel rental

`POST /rentals/{rental_id}/cancel`

Customer JWT. Revokes remote access and runs the existing rental-end /
phone-service release path. Same ownership rules as remote access.

---

## Concurrency (frontend implications)

- Slot 1 and Slot 2 are independent. Two customers can stream at once.
- Calling start-session twice for the same rental reuses one GADS session.
- Keep one stream reader; send controls on other requests.
- Serialize tap/swipe/type/back/home/recents/notification_shade/quick_settings/rotate/settings
on the client if you want strict ordering; the backend also serializes control per rental.

---

## Minimal frontend sequence

```
POST /rentals/{id}/remote-access
GET  /rentals/{id}/remote-access/device-status
GET  /rentals/{id}/remote-access/stream          // canvas
POST /rentals/{id}/remote-access/control         // tap | swipe | type | back | home | recents | notification_shade | quick_settings | rotate | settings
POST /rentals/{id}/esim/upload                   // optional, multipart qr_image
POST /rentals/{id}/remote-access/troubleshoot        // diagnose | reboot
POST /rentals/{id}/remote-access/reconnect-cellular  // 501 action_not_supported today
POST /rentals/{id}/remote-access/complete
```

