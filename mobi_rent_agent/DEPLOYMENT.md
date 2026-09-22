# Two-machine production deployment (VPS + physical PC)

**Confirmed architecture:** VoidFix inbound webhooks and the internet-facing HTTP
API run on the **Linux VPS**. All **ADB/USB** work stays on the **physical PC**
connected to the 20 phones. The VPS **never** accesses USB devices.

## Existing communication (before VPS split)

| Direction | Mechanism | Code |
|-----------|-----------|------|
| PC → cloud backend | Heartbeat POST | `HttpHeartbeatTransport` → `HEARTBEAT_ENDPOINT` or Supabase REST |
| PC → cloud backend | Provisioning pull | `HttpActivationJobSource` → `PROVISIONING_ENDPOINT/claim` |
| PC → VoidFix | Outbound SMS / delivery poll | `SmsDispatchService` → `VoidFixSmsGateway` |
| VoidFix → you | Inbound webhook | Was local test listener; **production → VPS** `vps_backend_server.py` |

There was **no** existing VPS→PC command channel for SMS. The smallest added
interface is an authenticated **farm status HTTP API** on the PC:

```
VPS (vps_backend_server.py)
  GET /farm/status  ──Bearer──►  PC (farm_agent_status_server.py)
                                      GET /agent/health  → adb devices + slot_map
```

Future outbound SMS from the portal should **queue on VPS/backend** and be
**claimed by the PC agent** (same pattern as provisioning), not moved to the VPS.

---

## Component split

### VPS (`MOBI_RENT_DEPLOY_ROLE=vps`)

| Component | Process |
|-----------|---------|
| Public `/health` | `tools/vps_backend_server.py` |
| VoidFix inbound webhook | same (`POST /voidfix/inbound`) |
| Inbound persistence | `logs/inbound_messages.sqlite` |
| Slot mapping for inbound | `voidfix_devices.json` (copy; **do not edit IDs**) |
| Farm reachability | `GET /farm/status` → proxies to PC |
| systemd | `mobi-rent-vps-backend.service` |

**Not on VPS:** `main.py`, ADB, `slot_map.json` (serials), SMS send/outbox (stays on PC).

### Physical PC (`MOBI_RENT_DEPLOY_ROLE=farm`)

| Component | Process |
|-----------|---------|
| Heartbeat, provisioning, health, proxy | `main.py` → `mobi-rent-farm-agent.service` |
| VoidFix SMS send + delivery poll + outbox | `SmsDispatchService` (existing; not auto-send in daemon) |
| ADB slot status | `AdbSlotStatusProvider` |
| Authenticated status for VPS | `tools/farm_agent_status_server.py` → `mobi-rent-farm-status.service` |

**On PC disk (gitignored):** `.env`, `slot_map.json`, `voidfix_devices.json`, `logs/sms_outbox.sqlite`

---

## Ports and firewall

| Host | Port | Bind | Exposure |
|------|------|------|----------|
| VPS | 443 | nginx/Caddy | **Public** (VoidFix webhook HTTPS) |
| VPS | 8080 | 127.0.0.1 | Local only; reverse-proxy to 443 |
| PC | 8790 | private IP / VPN | **VPS only** — `FARM_AGENT_API_TOKEN` |
| PC | — | — | ADB USB; no inbound port for phones |

Use WireGuard/VPN between VPS and PC so `FARM_AGENT_URL` is a private address
(for example `http://10.0.0.2:8790`).

---

## Install paths

Both hosts: `/opt/mobi-rent-agent` (or equivalent), Python **3.10+**, venv:

```bash
bash scripts/linux/bootstrap_venv.sh
```

Templates:

- VPS: `config/deploy/env.vps.example` → `.env`
- PC: `config/deploy/env.farm.example` → `.env`

Validate:

```bash
python tools/validate_production_config.py --role vps
python tools/validate_production_config.py --role farm
```

---

## systemd

**VPS**

```bash
sudo cp systemd/mobi-rent-vps-backend.service /etc/systemd/system/
sudo systemctl enable --now mobi-rent-vps-backend
```

**Physical PC**

```bash
sudo cp systemd/mobi-rent-farm-agent.service /etc/systemd/system/
sudo cp systemd/mobi-rent-farm-status.service /etc/systemd/system/
sudo systemctl enable --now mobi-rent-farm-agent mobi-rent-farm-status
```

Legacy unit name `mobi-rent-agent.service` is equivalent to `mobi-rent-farm-agent.service`.

---

## HTTPS (VPS)

Point VoidFix dashboard webhook to:

`https://<your-domain>/voidfix/inbound`

nginx example:

```nginx
location /voidfix/inbound {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
location /health {
    proxy_pass http://127.0.0.1:8080;
}
```

---

## Secure VPS ↔ PC authentication

Set the **same** long random `FARM_AGENT_API_TOKEN` in both `.env` files.

- PC `farm_agent_status_server` requires `Authorization: Bearer <token>`.
- VPS `GET /farm/status` uses that token when calling the PC.

Optional: `VOIDFIX_WEBHOOK_SECRET` on VPS for inbound POST verification.

---

## Files to transfer securely

| File | VPS | PC |
|------|-----|-----|
| `.env` | Yes (VPS keys + token) | Yes (farm keys + token + VoidFix) |
| `voidfix_devices.json` | Yes (read-only copy) | Yes (canonical) |
| `slot_map.json` | No | Yes |
| `logs/sms_outbox.sqlite` | No | Yes (do not delete) |
| `logs/inbound_messages.sqlite` | Created on VPS | No |

**Never commit:** `.env`, API keys, tokens, `slot_map.json`, `voidfix_devices.json`, sqlite logs.

---

## Smoke checks (no SMS)

**PC:** `adb devices` — 20 serials; `python main.py` starts without sending SMS.

**PC:** `curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/agent/health`

**VPS:** `curl http://127.0.0.1:8080/health`

**VPS:** `curl http://127.0.0.1:8080/farm/status` (needs VPN + PC service)

**VPS:** POST test webhook body to `/voidfix/inbound` (use captured form sample in tests)

---

## Non-production paths

Do not deploy: `_tmp_*`, `backups/`, `config/prototype/`, legacy
`mobi-rent-voidfix-webhook.service` on PC if webhook is VPS-only.

Deployment is **prepared**, not **complete**, until these steps run on real hosts.
