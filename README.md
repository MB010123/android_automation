# Mobi-Rent Hardware Agent

Python background daemon that runs on the on-prem PC/server, tracks the
status of each ADB-connected Android device ("slot"), and sends a
periodic heartbeat to the Mobi-Rent backend so the portal always knows
which slots are `online`, `busy`, or `networkerror`.

The heartbeat daemon covers the verified Phase 1 scope:

- Daemon running on boot
- Secure connection to the Lovable backend API
- Heartbeat loop mapped to the existing payload schema
  (`hardware_agent_token`, `slot_id`, `status`)
- Graceful handling of connection drop-outs (auto-retry, no crash)

Phase 2 host orchestration includes eSIM provisioning, full-device SOCKS5
route reconciliation, and isolated radio/network recovery. Each component
is disabled until its required configuration and device capability are
present.

## Quick command reference

Run all commands from the `mobi_rent_agent` directory.

```bash
# One-time local setup
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
cp slot_map.example.json slot_map.json

# Confirm the phone is visible, then edit .env and slot_map.json
adb devices -l

# Hot run in the foreground
source .venv/bin/activate
python main.py

# Tests
python -m pytest tests/ -v

# Follow the application log in another terminal
tail -f logs/agent.log
```

Stop a foreground run with `Ctrl+C`. For an always-on production install,
use the [systemd instructions](#run-on-boot-systemd).

## Architecture

Clean Architecture / Ports & Adapters, so business logic never depends
on HTTP, ADB, or file I/O directly:

```
mobi_rent_agent/
├── domain/                  # Framework-free entities & interfaces
│   ├── models.py            #   SlotStatus, SlotState, HeartbeatPayload, HeartbeatResult
│   └── ports.py             #   HeartbeatTransport, SlotStatusProvider, Clock (abstract)
│
├── application/              # Use cases — orchestrate domain via ports only
│   ├── heartbeat_service.py #   HeartbeatService: the boot -> check -> ping -> retry loop
│   ├── provisioning_service.py # Per-slot activation orchestration
│   ├── proxy_service.py     #   Persistent one-route-per-slot reconciliation
│   ├── health_service.py    #   Radio/network monitoring and isolated recovery
│   ├── slot_coordinator.py  #   Cross-service per-slot operation mutexes
│   └── retry_policy.py      #   Exponential backoff policy
│
├── infrastructure/            # Concrete adapters (the only layer that
│   │                          #  knows about requests/adb/env files)
│   ├── config.py            #   Loads .env into AgentConfig
│   ├── api_client.py        #   HttpHeartbeatTransport (implements HeartbeatTransport)
│   ├── adb_slot_status.py   #   AdbSlotStatusProvider (implements SlotStatusProvider)
│   ├── adb_provisioner.py   #   Secure ADB-forwarded companion protocol
│   ├── activation_payload.py #  Direct string / HTTPS QR decoding
│   ├── adb_proxy.py         #   Android VPN companion route adapter
│   ├── adb_health.py        #   Radio/network probes and isolated reboot
│   ├── proxy_routes.py      #   Strict persistent route configuration
│   ├── system_clock.py      #   SystemClock (implements Clock)
│   ├── provisioning_api.py  #   Backend activation claim/result adapter
│   └── logging_setup.py     #   Rotating file + console logging
│
├── tests/
│   └── test_heartbeat_service.py  # Use case tested with in-memory fakes, no network/ADB
│
├── main.py                    # Composition root: wires adapters into the use case
├── slot_map.example.json      # slot_id -> fixed ADB serial mapping (copy to slot_map.json)
├── .env.example                # Copy to .env and fill in HARDWARE_AGENT_TOKEN
├── requirements.txt
├── requirements-dev.txt
└── systemd/mobi-rent-agent.service
```

**Dependency rule:** `domain` has zero imports from `application` or
`infrastructure`. `application` imports only `domain`. `infrastructure`
implements `domain.ports` and is wired up in `main.py`. This means the
whole heartbeat loop is unit-tested (see `tests/`) without touching a
real network socket or the `adb` binary, and the HTTP/ADB adapters can
be swapped later (e.g. a future `MockTransport` for staging) without
touching `HeartbeatService`.

## How it maps to the client's payload schema

`POST https://mobi-rent.lovable.app/api/public/hardware/queue`
Header: `Authorization: Bearer <HARDWARE_AGENT_TOKEN>`

```json
{
  "hardware_agent_token": "SGd_566Y54$3rrwf$",
  "slot_id": 1,
  "status": "online"
}
```

- `hardware_agent_token` — read once from `.env`, reused for every slot.
- `slot_id` — resolved from `slot_map.json` (fixed physical identity,
  1-20), independent of USB enumeration order.
- `status` — `online` (ADB device present + responsive), `networkerror`
  (offline/unauthorized/unresponsive). `busy` is reserved for Phase 2's
  job-lock logic and is modeled in `SlotStatus` today so no schema
  changes are needed later.

## Requirements

- Linux host (Ubuntu or Debian is recommended for the systemd setup)
- Python 3.10 or newer, including the `venv` module
- Android Platform Tools (`adb`)
- USB data cable and a host USB port for each managed phone
- Android developer options and USB debugging enabled on every phone
- Backend values for `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and
  `HARDWARE_AGENT_TOKEN`
- Internet access from the host to the configured backend endpoint

Install the base packages on Ubuntu or Debian:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip adb android-sdk-platform-tools-common
python3 --version
adb version
```

The `android-sdk-platform-tools-common` package provides common Android udev
rules. On another Linux distribution, install its Android platform-tools and
udev-rules equivalents.

## Local setup

From the repository root:

```bash
cd mobi_rent_agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
cp slot_map.example.json slot_map.json
```

The shell prompt normally shows `(.venv)` while the virtual environment is
active. In each new terminal, return to this directory and run:

```bash
source .venv/bin/activate
```

### Connect and authorize Android devices

On each phone:

1. Open **Settings > About phone** and tap **Build number** seven times.
2. Open **Settings > System > Developer options** and enable **USB debugging**.
3. Connect the phone with a USB data cable.
4. Accept the **Allow USB debugging** prompt on the phone. Enable the
   remember option when the device is dedicated to this host.

Restart ADB and list the connected devices:

```bash
adb kill-server
adb start-server
adb devices -l
# List of devices attached
# R58N123ABCD    device product:oriole model:Pixel_6 transport_id:1
```

Every managed phone must show the state `device`. If it shows `unauthorized`,
unlock the phone and accept the authorization prompt. If no device appears,
try a known data-capable cable and check the USB/udev troubleshooting section
below.

### Configure slot identity

Map each permanent physical slot ID to the exact serial shown by `adb devices`.
For example, edit `slot_map.json` to contain:

```json
{
  "1": "R58N123ABCD",
  "2": "R58N456EFGH"
}
```

Slot IDs must be integers and serials must remain tied to the same physical
positions. Do not use USB enumeration order as identity.

The daemon defaults to the `slot_map.json` beside `main.py`. If the
authoritative map is elsewhere, set an absolute `SLOT_MAP_PATH`. Do not keep
two independently edited production maps.

### Configure the backend

Open `.env` and fill in at least these values:

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-supabase-anon-key
SUPABASE_TABLE=hardware_queue
HARDWARE_AGENT_TOKEN=your-agent-token

HEARTBEAT_INTERVAL_SECONDS=15
REQUEST_TIMEOUT_SECONDS=10
LOG_LEVEL=INFO
ADB_PATH=adb
SLOT_MAP_PATH=
```

`SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `HARDWARE_AGENT_TOKEN` are required at
startup. Keep `.env` private; it is ignored by Git. The token must match the
hardware agent registered by the backend. To generate a new random value when
setting up both sides of a new integration:

```bash
openssl rand -hex 32
```

By default, heartbeats are posted directly to:

```text
{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}
```

To use a dedicated API route instead, add `HEARTBEAT_ENDPOINT` to `.env`. The
dedicated route is authenticated with `HARDWARE_AGENT_TOKEN`; direct Supabase
REST is authenticated with `SUPABASE_ANON_KEY`. The three required values must
still be present because configuration validation occurs before endpoint
selection.

```dotenv
HEARTBEAT_ENDPOINT=https://mobi-rent.example/api/public/hardware/queue
```

Phase 2 features are off by default. Leave `PROVISIONING_ENDPOINT` and
`PROXY_ROUTES_PATH` blank and `HEALTH_MONITOR_ENABLED=false` for the initial
heartbeat-only run.

## Hot run (foreground)

Use this mode for initial setup, development, and live troubleshooting:

```bash
cd mobi_rent_agent
source .venv/bin/activate
python main.py
```

The daemon will loop forever, sending one heartbeat per configured slot
every `HEARTBEAT_INTERVAL_SECONDS` (default 15s), backing off
exponentially (capped at 60s) while the backend or network is
unreachable, and recovering automatically once it's reachable again.
Stop with `Ctrl+C` (SIGINT) — it shuts down gracefully.

The same logs are printed to the terminal and written to
`logs/agent.log`. In a second terminal:

```bash
cd mobi_rent_agent
tail -f logs/agent.log
```

### Verify the first run

1. Confirm startup does not report a missing configuration or slot-map error.
2. Confirm every configured serial still appears as `device` in
   `adb devices -l`.
3. Look for successful heartbeat responses in the terminal or application log.
4. Confirm the backend receives one row/request per configured slot with
   `online` status.
5. Disconnect one test phone and confirm its next heartbeat becomes
   `networkerror`; reconnect and authorize it, then confirm it returns to
   `online`.

For a quick process check from another terminal:

```bash
pgrep -af 'python.*main.py'
```

Do not start a second agent against the same slot map. A hot run and the
systemd service must not run at the same time.

## Phase 2 provisioning pipeline

Provisioning is disabled unless `PROVISIONING_ENDPOINT` is configured. The
worker atomically claims work with:

```http
POST {PROVISIONING_ENDPOINT}/claim
Authorization: Bearer {HARDWARE_AGENT_TOKEN}
X-Hardware-Agent-Token: {HARDWARE_AGENT_TOKEN}

{"slot_ids":[1,2,3]}
```

The response may be a JSON array or `{"jobs":[...]}`. A job must contain
exactly one of `activation_code` or an HTTPS `qr_url`:

```json
{
  "jobs": [{
    "job_id": "activation-123",
    "slot_id": 1,
    "qr_url": "https://storage.example/activation-123.png",
    "switch_after_download": true
  }]
}
```

The backend claim operation must lease each job atomically and use `job_id`
as its idempotency key. Failed result acknowledgements are retained and
retried on later polling cycles. Terminal outcomes are sent to:

```http
POST {PROVISIONING_ENDPOINT}/{job_id}/result

{"job_id":"activation-123","slot_id":1,"success":true,"device_code":0,"active_phone_number":"+15551234567"}
```

Jobs for separate slots run concurrently; jobs for the same slot run in
order. An exception from one device is converted to a failed result and
cannot terminate another slot's worker.

QR images are downloaded with a timeout and 5 MiB limit, decoded locally
with ZXing, and rejected unless they contain an `LPA:` activation string.
No camera or Android UI automation is used. Before provisioning, the host
checks ADB state, boot completion, and the Android eUICC feature.

### On-device companion requirement

Zero-touch installation cannot be guaranteed by launching the public LPA
UI intent. Each managed image therefore needs a privileged companion that:

1. listens on the local abstract socket configured by
   `PROVISIONING_COMPANION_SOCKET`;
2. accepts one newline-delimited JSON request from the host;
3. constructs `DownloadableSubscription.forActivationCode(...)`;
4. calls `EuiccManager.downloadSubscription(...)`; and
5. returns its asynchronous callback as one JSON line, for example
   `{"success":true,"device_code":0}`.

The companion must have carrier privileges or the system
`WRITE_EMBEDDED_SUBSCRIPTIONS` permission. It must treat a resolvable result
as a failure rather than opening consent UI. It must also cache terminal
results by `job_id`, so a lease retry returns the existing outcome instead
of downloading the same profile twice. The host sends activation codes
through an ADB-forwarded local socket, never through `adb shell` arguments
or logs.

The active phone number callback is best-effort: Android/carrier profiles
do not always expose the MSISDN. When unavailable, provisioning can still
succeed but `active_phone_number` will be omitted.

## Phase 2 SOCKS5 isolation

Copy `proxy_routes.example.json` to the ignored `proxy_routes.json`, define
one assignment for every managed slot, and set:

```dotenv
PROXY_ROUTES_PATH=proxy_routes.json
NETWORK_COMPANION_SOCKET=mobi_rent.network
```

Startup fails if any managed slot is missing, an unknown slot is present,
or two slots share the same host/port/account assignment. The reconciler
continually sends each route only to that slot's ADB serial. Credentials
are not passed in command-line arguments.

Android does not provide a system-wide SOCKS5 setting. The privileged
device companion must run a `VpnService`/tun2socks implementation and
support this JSON-line command on `mobi_rent.network`:

```json
{
  "command": "ensure_socks5_route",
  "slot_id": 1,
  "host": "proxy-slot-01.example.net",
  "port": 1080,
  "username": "slot01",
  "password": "...",
  "persistent": true,
  "block_on_disconnect": true
}
```

It must persist the assignment, restore it after boot, enable always-on VPN
with connection blocking, and reply with
`{"success":true,"active":true,"slot_id":1}` only after the tunnel is
active. This provides operational route isolation; it is not traffic
obfuscation and does not attempt to evade carrier or platform controls.

## Phase 2 health and recovery

After chassis testing, enable:

```dotenv
HEALTH_MONITOR_ENABLED=true
HEALTH_INTERVAL_SECONDS=15
HEALTH_FAILURE_THRESHOLD=3
HEALTH_REBOOT_COOLDOWN_SECONDS=300
```

Each slot is independently checked for ADB state, completed boot, carrier
registration from `telephony.registry`, and network reachability. Three
consecutive unhealthy observations trigger `adb -s SERIAL reboot` for only
that slot. A five-minute cooldown prevents reboot loops, only one reboot is
issued at a time, and recovery is deferred while that slot is provisioning
or changing routes.

An ADB reboot cannot recover a board whose USB/ADB transport is completely
gone. Guaranteed recovery from that condition requires a separately
controllable USB hub or chassis power API; the current hardware interface
must be confirmed before adding that adapter.

## Pixel 6 chassis validation

Software cannot remove RF coupling caused by twenty internal antennas in a
closed chassis. Before enabling automatic recovery in production:

1. activate slots gradually rather than all at once;
2. record registration success, signal summaries, reconnect rate, heat,
   and power with 1, 5, 10, and 20 active boards;
3. tune failure thresholds from measured behavior; and
4. reconsider the chassis if sustained registration degrades under load.

Pixel 6 launched with Android 12; use the supplier-supported Android
12-or-newer image rather than assuming Android 11 compatibility.

## Run on boot (systemd)

The included unit expects the application at `/opt/mobi-rent-agent`, a service
account named `mobirent`, and its Python environment at
`/opt/mobi-rent-agent/.venv`. Stop any foreground copy before continuing.

### 1. Install the application

Run these commands from the `mobi_rent_agent` project directory:

```bash
sudo systemctl stop mobi-rent-agent 2>/dev/null || true
sudo useradd --system --create-home --shell /usr/sbin/nologin mobirent 2>/dev/null || true
sudo mkdir -p /opt/mobi-rent-agent
sudo cp -a application domain infrastructure main.py requirements.txt \
  slot_map.json .env systemd /opt/mobi-rent-agent/
sudo python3 -m venv /opt/mobi-rent-agent/.venv
sudo /opt/mobi-rent-agent/.venv/bin/python -m pip install --upgrade pip
sudo /opt/mobi-rent-agent/.venv/bin/python -m pip install \
  -r /opt/mobi-rent-agent/requirements.txt
sudo mkdir -p /opt/mobi-rent-agent/logs
sudo chown -R mobirent:mobirent /opt/mobi-rent-agent
sudo chmod 600 /opt/mobi-rent-agent/.env
```

When Phase 2 proxy routing is enabled, also copy `proxy_routes.json`, set
`PROXY_ROUTES_PATH=/opt/mobi-rent-agent/proxy_routes.json`, and protect that
file with mode `600` because it contains credentials. If the slot map is kept
elsewhere, use an absolute `SLOT_MAP_PATH` readable by `mobirent`.

### 2. Verify ADB as the service account

ADB authorization and its server are user-specific. Stop a server started by
another account, start it as `mobirent`, and verify access:

```bash
adb kill-server
sudo -u mobirent -H adb start-server
sudo -u mobirent -H adb devices -l
sudo -u mobirent -H adb -s YOUR_SERIAL shell echo ok
```

Unlock each device and accept the new RSA authorization prompt if Android asks
again. All configured devices must appear as `device` for the `mobirent`
account before enabling the daemon.

### 3. Enable and start the service

```bash
sudo cp systemd/mobi-rent-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mobi-rent-agent
sudo systemctl status mobi-rent-agent --no-pager
sudo journalctl -u mobi-rent-agent -f
```

Install Android USB udev rules and ensure the `mobirent` service account can
open every device. Start the ADB server as that same account; do not share a
root-owned ADB server with the daemon.

Useful service commands:

```bash
sudo systemctl restart mobi-rent-agent       # restart after config/code changes
sudo systemctl stop mobi-rent-agent          # stop the daemon
sudo systemctl start mobi-rent-agent         # start the daemon
sudo systemctl disable --now mobi-rent-agent # stop and disable boot startup
sudo journalctl -u mobi-rent-agent -n 200 --no-pager
```

After changing `.env`, `slot_map.json`, or application code under `/opt`, run
`sudo systemctl restart mobi-rent-agent`. A `daemon-reload` is only needed
after changing the `.service` file itself.

## Troubleshooting

### Required configuration is not set

Run from the directory containing `.env`, or verify that systemd can read
`/opt/mobi-rent-agent/.env`. Do not put spaces around `=` in environment-file
assignments. Check only variable names, without printing secrets:

```bash
grep -E '^[A-Z0-9_]+=' .env | cut -d= -f1
```

### Slot map is missing or invalid

Confirm `slot_map.json` exists beside `main.py`, or set `SLOT_MAP_PATH` to an
absolute path. Validate its JSON syntax:

```bash
python -m json.tool slot_map.json >/dev/null && echo 'slot map JSON is valid'
```

### ADB device is missing or reports `no permissions`

```bash
lsusb
adb kill-server
sudo udevadm control --reload-rules
sudo udevadm trigger
adb start-server
adb devices -l
```

Reconnect the cable after reloading rules. Verify that Android USB mode and the
cable support data, not charging only. Do not solve this by running the agent
as root; fix udev access for the service account.

### ADB device is `unauthorized`, `offline`, or `networkerror`

For `unauthorized`, unlock the phone and accept the USB debugging prompt. If
the prompt is stale, revoke USB debugging authorizations in Developer options,
reconnect, and authorize again. For `offline`, restart ADB and reconnect the
device. The agent reports `networkerror` whenever a mapped serial is absent,
not in `device` state, or fails the `adb shell echo ok` probe.

### Heartbeats return HTTP errors

- `401` or `403`: verify the anon key for direct Supabase mode, or the hardware
  token for a dedicated `HEARTBEAT_ENDPOINT`.
- `404`: verify `SUPABASE_URL`, `SUPABASE_TABLE`, or the dedicated endpoint.
- Timeout/connection error: test DNS, TLS, firewall, and outbound connectivity
  from the host and from the `mobirent` account.
- Supabase row-level-security error: ensure the backend policy permits the
  intended heartbeat insert or use the dedicated backend route.

Never paste `.env`, proxy credentials, tokens, or full authenticated HTTP
commands into an issue or shared log.

### systemd service keeps restarting

Inspect the exit reason and effective unit configuration:

```bash
sudo systemctl status mobi-rent-agent --no-pager
sudo journalctl -u mobi-rent-agent -n 200 --no-pager
sudo systemctl cat mobi-rent-agent
sudo -u mobirent -H /opt/mobi-rent-agent/.venv/bin/python \
  /opt/mobi-rent-agent/main.py
```

Stop the service before running the last command for an extended hot-run test.

## Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

The suite uses in-memory fakes for backend, device, proxy, health, and time
ports. It covers heartbeat resilience, real QR decoding, per-slot
provisioning isolation, route uniqueness, proxy failure isolation, recovery
thresholds, cooldowns, and busy-slot protection without touching real
devices.

## Security notes

- `HARDWARE_AGENT_TOKEN` lives only in `.env` (git-ignored) — never
  hard-coded, never logged.
- The HTTP client only ever sends the fields in the agreed schema.
- All network/ADB failures are caught at the infrastructure boundary
  and turned into typed `HeartbeatResult` values; the daemon process
  itself never crashes from a dropped connection or an offline device.
