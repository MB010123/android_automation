# VPS → Lovable server API contract

The VPS does **not** use `SUPABASE_SERVICE_ROLE_KEY` in production. Tenant data
stays in Lovable/Supabase. The VPS calls these named Lovable server endpoints
with `VPS_TO_LOVABLE_API_TOKEN`.

This document is the contract Lovable must implement. The VPS client already
targets these paths. The Lovable handlers are **not implemented in this repo**.

## Authentication

```
Authorization: Bearer <VPS_TO_LOVABLE_API_TOKEN>
```

Optional: `X-Request-Id`, `Idempotency-Key` on mutating POSTs.

Reject missing/invalid tokens with 401. Do not accept:

- Supabase anon key
- user access tokens
- `FARM_SERVICE_TOKEN`
- `FARM_AGENT_API_TOKEN`
- `VOIDFIX_WEBHOOK_SECRET`

as substitutes.

## Endpoints

Base URL: `LOVABLE_API_URL` (example: `https://app.loanerphones.com/api/vps`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/slots/by-bay/{bay}` | Authoritative slot row (`user_id`, `imei2`, `carrier_name`, …) |
| GET | `/slots?user_id=` | Slots owned by that user |
| GET | `/slots/{slot_id}` | Resolve Lovable `slots.id` → bay |
| POST | `/slots/by-bay/{bay}/claim` | Claim/assign ownership after validating profile + current owner |
| GET | `/profiles/{user_id}` | 200 if profile exists, 404 otherwise |
| PUT | `/profiles/{user_id}` | Ensure profile (`{ "email" }`) |
| GET | `/messages/{message_id}` | Authoritative tenant message |
| GET | `/slots/by-bay/{bay}/messages` | Tenant-visible messages (`limit`, `direction`) |
| POST | `/messages` | Create inbound/outbound tenant message |
| PATCH | `/messages/{message_id}` | Update message status |
| POST | `/esim-uploads` | Record eSIM storage key for a slot |

No generic SQL/RPC endpoint.

## Claim rules

`POST /slots/by-bay/{bay}/claim` body:

```json
{ "user_id": "<uuid>", "rental_id": "<uuid-or-null>" }
```

- 400 if `user_id` is missing or has no profile
- 409 if another user already owns the slot
- 200 if unowned or already owned by the same user
- Do not accept client `imei2` as an overwrite of another slot

## Slot body

Responses must include authoritative:

- `imei2`
- `carrier_name`
- `user_id`
- `motherboard_slot_num`

The VPS does not encode Verizon/T-Mobile/AT&T purchase rules.

## Trust boundaries

| Credential | Direction | Purpose |
|---|---|---|
| Supabase user access token | Browser → VPS | User identity |
| `VPS_TO_LOVABLE_API_TOKEN` | VPS → Lovable | Tenant reads/writes |
| `FARM_SERVICE_TOKEN` | Lovable/server → VPS | Farm/operator API |
| `FARM_AGENT_API_TOKEN` | VPS → Farm Agent | Device control |
| `VOIDFIX_WEBHOOK_SECRET` | VoidFix → VPS | Inbound webhook |
