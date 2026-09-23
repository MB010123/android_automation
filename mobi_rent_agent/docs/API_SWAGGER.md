# VPS API — Swagger / OpenAPI

The VPS backend (`tools/vps_backend_server.py`) uses Python’s stdlib `http.server`. OpenAPI is generated in code (`infrastructure/vps_openapi_spec.py`) and served without changing business logic.

## URLs (default listen `127.0.0.1:8080`)

| Resource | Path |
|----------|------|
| Swagger UI | [http://127.0.0.1:8080/docs](http://127.0.0.1:8080/docs) |
| OpenAPI JSON | [http://127.0.0.1:8080/openapi.json](http://127.0.0.1:8080/openapi.json) |
| ReDoc | [http://127.0.0.1:8080/redoc](http://127.0.0.1:8080/redoc) |

Documentation endpoints are **unauthenticated** (read-only spec). Calling protected APIs from Swagger UI still requires a bearer token.

## Authentication

### Lovable / server → VPS (management + SMS)

```http
Authorization: Bearer <FARM_SERVICE_TOKEN>
```

### VoidFix → VPS inbound webhook

Optional shared secret when `VOIDFIX_WEBHOOK_SECRET` is set:

- Header `X-Voidfix-Webhook-Secret` or `X-Webhook-Secret`
- Query `?secret=...`

Not the Farm service token.

### VPS → Lovable (outbound, external)

After `POST /voidfix/inbound`, VPS may POST normalized JSON to `LOVABLE_INBOUND_WEBHOOK_URL` with:

```http
X-Mobi-Rent-Signature: <hex HMAC-SHA256 of raw body>
```

Secret on both sides: `<LOVABLE_INBOUND_WEBHOOK_HMAC_SECRET>`.  
Lovable implements e.g. `POST /api/public/farm/inbound-sms` — **not** hosted on the VPS.

### Hardware queue (external)

`GET` / `POST /api/public/hardware/queue` on Lovable/Supabase is used by the **farm hardware agent daemon** (`main.py`), not by `vps_backend_server.py`. It is described in OpenAPI under **Integration** for reference only.

## Development

```bash
cd mobi_rent_agent
python tools/vps_backend_server.py --host 127.0.0.1 --port 8080
```

Open `/docs`, click **Authorize**, enter `Bearer <FARM_SERVICE_TOKEN>` (use a dev token from your local `.env`, never commit it).

Example (available slots):

```bash
curl -s -H "Authorization: Bearer <FARM_SERVICE_TOKEN>" \
  http://127.0.0.1:8080/farm/slots/available
```

## Async jobs

`POST /farm/slots/{bay}/assign` and `POST /slots/{slot_id}/actions/{action}` return **202** with `{ "job_id": "..." }`. Poll:

```bash
curl -s -H "Authorization: Bearer <FARM_SERVICE_TOKEN>" \
  http://127.0.0.1:8080/jobs/<job_id>
```

States: `pending` → `running` → `done` or `failed`. A **202** does not mean Android work has finished.

## Security notes

- OpenAPI examples use placeholder UUIDs and `+1555…` numbers.
- Spec must not contain real tokens, HMAC secrets, Tailscale IPs, or production serials.
- SMS send idempotency: `(farm_slot, idempotency_key)` with matching `to`+`body`.
- Assign idempotency: `assign-{rental_id}`.
- Actions `airplane_cycle` and `voidfix_repair` may complete as failed jobs with `action_not_supported` until Farm capabilities exist.

## Tests

```bash
pytest tests/test_vps_openapi.py -q
```
