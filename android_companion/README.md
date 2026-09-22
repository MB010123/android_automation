# Mobi-Rent Android Companion

On-device companion for the Phase 1 host daemon. Talks to the host over
ADB-forwarded abstract sockets. **Dry-run by default:** no real eSIM
download, no production SOCKS5, no companion-originated backend heartbeat
unless compiled in.

## Sockets

| Abstract socket | Purpose |
|-----------------|---------|
| `mobi_rent.companion` | `get_identity`, `get_health`, `assign_slot`, `ping` |
| `mobi_rent.provisioning` | `provision_esim` (dry-run), `get_esim_status` |
| `mobi_rent.network` | `ensure_socks5_route` (dry-run), `start_test_vpn`, `stop_vpn`, `vpn_status` |

Commands that include `slot_id` are rejected if they do not match the
slot assigned to this device.

## Privileges

| Operation | Requirement |
|-----------|-------------|
| Detect eUICC | Normal app (`EuiccManager.isEnabled`) |
| Install eSIM | `WRITE_EMBEDDED_SUBSCRIPTIONS` or carrier privileges. User-consent UI is treated as failure. **Disabled. `get_esim_status` reports `can_silent_install`.** |
| Test VPN | One-time user VPN consent (`VpnService.prepare`) |
| Production SOCKS5 / always-on / block-on-disconnect | Privileged/device-owner + tun2socks. **Disabled.** |
| USB serial | `READ_PRIVILEGED_PHONE_STATE`. App uses `ANDROID_ID` as the stable ID. |

## Build

Requires JDK 17 and Android SDK 34.

```bash
cd android_companion
./gradlew :app:assembleDebug :app:testDebugUnitTest
adb -s SERIAL install -r app/build/outputs/apk/debug/app-debug.apk
adb -s SERIAL shell am start -n com.mobirent.companion/.MainActivity --ei slot_id 1
```

From `mobi_rent_agent/`:

```bash
python tools/companion_probe.py --slot 1 --assign
python tools/companion_single_device_test.py --slot 1 --apk ..\android_companion\app\build\outputs\apk\debug\app-debug.apk
```

After the single-device test passes, install on the rest of the farm (does not enable real eSIM or production SOCKS5):

```bash
python tools/companion_deploy.py --apk ..\android_companion\app\build\outputs\apk\debug\app-debug.apk
```
