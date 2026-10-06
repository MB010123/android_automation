# In-app controlled setup session (Lovable frontend contract)

Customer stays on LoanerPhones. GADS is the VPS-side engine only.
The browser must never open `gads.loanerphones.com`, receive GADS credentials,
or name a bay/serial/UDID/workspace.

## UI states

1. `upload_qr` — existing `POST /rentals/{rental_id}/esim/upload`
2. `connecting` — `POST /rentals/{rental_id}/remote-access` then show spinner
3. `live_phone_screen` — `<img>` / MJPEG from `GET {stream_path}` with customer JWT
4. `esim_setup` — overlay copy: complete Android eSIM confirmation on the Pixel
5. `voidfix_approval` — overlay copy: set VoidFix as default SMS app when Android asks
6. `phone_ready` — `POST .../complete` returned `ui_state=phone_ready`; hide stream
7. `cancelled` — `POST /rentals/{rental_id}/cancel`

## APIs (customer JWT)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/rentals/{rental_id}/esim/upload` | unchanged QR multipart `qr_image` |
| POST | `/rentals/{rental_id}/remote-access` | 201 `{session_mode:in_app, stream_path, allowed_controls, ui_state}` — **no** `platform_login` |
| GET | `/rentals/{rental_id}/remote-access` | poll `activation_state`, `setup_phase`, `setup_complete` |
| GET | `/rentals/{rental_id}/remote-access/stream` | MJPEG proxy (`multipart/x-mixed-replace`) |
| POST | `/rentals/{rental_id}/remote-access/control` | `{action:tap\|swipe\|type\|back, x,y,x2,y2,text}` only |
| POST | `/rentals/{rental_id}/remote-access/complete` | 200 ready **only** if eSIM `confirmed` and VoidFix verified |
| POST | `/rentals/{rental_id}/remote-access/revoke` | close session without ending rental |
| POST | `/rentals/{rental_id}/cancel` | revoke + CLEANUP REQUIRED |

Never send: `slot_id`, `farm_slot_id`, `serial`, `udid`, `workspace_id`, `home`, `recents`, ADB, GADS URLs.

`403 forbidden` for wrong user / wrong rental / expired / cancelled.
`403 forbidden_control` for blocked gestures.
`409 setup_incomplete` if complete is called too early (`ui_state` tells which step).
`409 setup_state_blocked` if the Pixel left the allowed setup activities.

## Admin (Farm service token)

`POST /farm/slots/{bay}/cleanup-verified` after Niaozun/GADS/ADB physical check.
Until then the bay is omitted from `GET /farm/slots/available` and assign/reserve return `cleanup_required`.

## Manual first live test (prototype)

Do not deploy from this change set. Do not edit live GADS unless the VPS map is already installed.

Prerequisites:

1. Farm Agent on the Windows farm PC (`MobiRentFarmAgent` scheduled task) listening `0.0.0.0:8790`.
2. GADS hub + provider live; **unique workspace per bay 1–20** in `gads_workspaces.json` (never share one workspace).
3. VPS `REMOTE_ACCESS_POC_ENABLED=true`, `PROVISIONING_ALLOWED_SLOT_IDS=1` (legacy silent provision stays Slot 1), `REMOTE_ACCESS_WORKSPACE_MAP_PATH` installed, `VOIDFIX_ANDROID_PACKAGE` set, and **unset** `REMOTE_ACCESS_PREPARE_SLOT_IDS` / `REMOTE_ACCESS_OBSERVE_SLOT_IDS` (or they will keep complete/observe on Slot 1).
4. Lovable: customer JWT against `https://api.loanerphones.com`; remove GADS website redirect; use `stream_path` + `control`.
5. Pixel: Android eSIM UI and default-SMS role dialog reachable. No Device Owner / silent eSIM.

Steps: rent any mapped bay 1–20 → QR upload → POST remote-access → GET stream → customer eSIM confirm → customer VoidFix SMS approval → POST complete → session closed → phone ready.

Cancel: POST `/rentals/{id}/cancel` then confirm the bay is absent from available until `POST /farm/slots/{bay}/cleanup-verified`.


- Remove GADS hub-ui redirect / username / password screens.
- Render MJPEG from VPS `stream_path` with `Authorization: Bearer <customer JWT>`.
- Map pointer events to `control` taps/swipes in **device pixels** (not CSS pixels) after measuring the stream frame.
- Show only Back plus on-screen tapping; do not draw Home/Recents.
- After QR upload success, call `POST /remote-access` and go to connecting → live screen.
- Call `complete` when the customer taps “I finished setup”; handle 409 by showing `esim_setup` or `voidfix_approval`.
- Wire Cancel rental to `POST /rentals/{id}/cancel`.
