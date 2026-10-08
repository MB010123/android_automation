# Customer remote-phone API (frontend integration spec)

Status: backend contract for a future Lovable/customer UI.
Do **not** connect the browser to GADS, Farm Agent, ADB, or the Pixel.

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
  "allowed_controls": ["tap", "swipe", "type", "back", "home", "recents"],
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

| HTTP | `error` | When |
| --- | --- | --- |
| 401 | `unauthorized` | Missing/invalid customer JWT |
| 404 | `rental_not_found` | Unknown rental id |
| 403 | `rental_not_owned` | Signed-in user does not own the rental |
| 403 | `session_expired` | Rental end time has passed |
| 409 | `phone_offline` | Assigned Pixel is offline |
| 409 | `remote_access_busy` | **Another rental** currently holds the remote-access lease |
| 503 | `phone_unavailable` | Bay not mapped / remote access disabled |
| 502/503 | `gads_unavailable` | Screen service not configured or rejected the grant |

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
outside `0..8192` and rejects notification-shade pulls. Bottom-edge navigation
gestures are allowed. Home/Recents are also explicit semantic actions.

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
Recents / Back gestures). Do **not** send a swipe that starts in the
status-bar band and pulls down (notification shade); that returns 403
`forbidden_control`. Explicit Home/Recents use semantic `{ "action": "home" }`
/ `{ "action": "recents" }` — never Android keycodes.

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

No coordinates and no keycodes. The VPS maps these to GADS (Farm Agent
fallback uses the matching Android nav event internally).

Forbidden (do not send): notification shade, `keycode`, `keyevent`, `adb`,
`shell`, `command`, serials, workspace ids.

| HTTP | `error` |
| --- | --- |
| 422 | `invalid_control` (bad/missing numbers, unknown action, extra fields) |
| 403 | `forbidden_control` (shade/ADB/identity spoof/raw keys) |
| 409 | `remote_access_not_ready` |
| 403 | `session_expired` / `rental_not_owned` |

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

| `error` | Meaning | Frontend |
| --- | --- | --- |
| `unauthorized` | JWT missing/invalid | Re-auth |
| `rental_not_found` | Bad rental id | Leave screen |
| `rental_not_owned` | Wrong user | Leave screen |
| `phone_offline` | Pixel offline | Retry later; show offline |
| `phone_unavailable` | Bay/device not ready | Support / wait |
| `remote_access_busy` | Another rental owns the lease | Do not retry as if "phone busy from our stream" |
| `remote_access_not_ready` | No active session | Call start-session |
| `session_expired` | Rental or session ended | End UI |
| `gads_unavailable` | Screen service down | Retry with backoff |
| `invalid_control` | Bad tap/swipe/type body | Fix mapping |
| `forbidden_control` | Disallowed gesture | Ignore / don't send |
| `manual_esim_required` | Informational code; **does not** block remote access | Show "finish eSIM in Settings" |
| `setup_incomplete` | Legacy setup-finish warning | Do not treat as stream failure |

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

If eSIM observation was already `confirmed` and default-SMS verification
succeeded, `setup_complete` may be `true` and `ui_state` may be `phone_ready`.
Either way the remote session is closed.

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
  "native_resolution": { "width": 1440, "height": 3120 }
}
```

| Field | Meaning |
| --- | --- |
| `state` | `online` \| `offline` \| `unavailable` |
| `requires_manual_action` | Customer must finish eSIM in Android Settings/LPA. **Remote access remains allowed.** |
| `remote_access_available` | Phone online and no other rental holds the lease |
| `remote_access_busy` | Another rental owns the lease |
| `native_resolution` | Optional; otherwise use JPEG frame size |

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

1. Upload QR.
2. Start remote access (allowed even when `requires_manual_action` is true).
3. Customer opens Android Settings → SIMs → Add eSIM and scans the gallery QR.
4. Backend observes activation (`POST .../activation-status`).
5. `activation_state` becomes `ACTIVE` only after confirmed observation.

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
- Serialize tap/swipe/type/back/home/recents on the client if you want strict
  ordering; the backend also serializes control per rental.

---

## Minimal frontend sequence

```
POST /rentals/{id}/remote-access
GET  /rentals/{id}/remote-access/device-status
GET  /rentals/{id}/remote-access/stream          // canvas
POST /rentals/{id}/remote-access/control         // tap | swipe | type | back | home | recents
POST /rentals/{id}/esim/upload                   // optional, multipart qr_image
POST /rentals/{id}/remote-access/complete
```
