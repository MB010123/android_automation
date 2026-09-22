# Isolated prototype environment

This directory is **not** the 20-bay PhoneFarmBox production config.
Do not copy these files over `slot_map.json`, `voidfix_devices.json`, or
the production `.env`.

## Setup

```bash
cd mobi_rent_agent
cp config/prototype/prototype.env.example config/prototype/prototype.env
cp config/prototype/prototype_devices.example.json config/prototype/prototype_devices.json
```

Fill `adb_serial` and `voidfix_device_id` for the **Pixel 7a test device
only**. Do not put a farm bay serial in this file. Do not infer a VoidFix
ID from the ADB serial.

Leave `VOIDFIX_ENABLED=false` and `VOIDFIX_REAL_SEND_CONFIRMATION=false`
until a reviewer authorizes one REAL_TEST SMS.

Set `PROTOTYPE_MODE` to one of:

- `PRODUCTION_DISABLED` (default) — inspect config, send nothing
- `AUDIT` — read-only status
- `DRY_RUN` — simulate eSIM/SMS workflow, no mutations
- `REAL_TEST` — still refuses SMS until every gate below is true

## Commands

```bash
python tools/prototype_readiness.py --env prototype audit
python tools/prototype_readiness.py --env prototype dry-run --device prototype-device-1
python tools/prototype_readiness.py --env prototype device-status --device prototype-device-1
python tools/prototype_readiness.py --env prototype esim-status --device prototype-device-1
python tools/prototype_readiness.py --env prototype voidfix-status
python tools/prototype_readiness.py --env prototype report --device prototype-device-1
```

A real SMS additionally requires `PROTOTYPE_MODE=REAL_TEST`,
`VOIDFIX_ENABLED=true`, a rotated `VOIDFIX_API_KEY` in this prototype env
file, an explicit VoidFix mapping, a recipient allowlist,
`VOIDFIX_REAL_SEND_CONFIRMATION=true`, and:

```bash
python tools/prototype_readiness.py --env prototype send-test-sms --device prototype-device-1 --recipient +15555550100 --confirm
```

Do not run that command until Pixel 7a prototype acceptance is reviewed.
Do not proceed to the spare Pixel 6 or a farm slot yet.

## Device Owner

Companion package: `com.mobirent.companion`
Admin receiver: `com.mobirent.companion.DeviceAdminReceiver`

Android Device Owner enrollment happens during initial setup or after a
factory reset of **that test device**. Do not factory-reset a PhoneFarmBox
production phone. Installing the APK later does not make it Device Owner.

## Inbound SMS

VoidFix does not publish a receive URL. Inbound stays
`unsupported_pending_provider_documentation`. Webhook ingest remains a
parser-only component behind `VOIDFIX_WEBHOOK_SECRET`.
