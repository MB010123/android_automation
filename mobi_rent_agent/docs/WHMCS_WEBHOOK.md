# WHMCS billing webhook

WHMCS (`vps_sync.php`) POSTs lifecycle events to the VPS. The VPS verifies the
request, then updates the user's credit wallet and subscription tier in
Lovable/Supabase.

The browser never calls this endpoint. Do not put the identifier or HMAC secret
in Lovable, git, or frontend environment files.

## Endpoint

```
POST /api/webhooks/whmcs
Content-Type: application/json
X-WHMCS-Identifier: <WHMCS_WEBHOOK_IDENTIFIER>
X-WHMCS-Signature: <hex HMAC-SHA256 of the raw JSON body>
```

`X-WHMCS-Signature` may be a bare hex digest or `sha256=<hex>`. Query-string
secrets are rejected.

Configure on the VPS only:

```
WHMCS_WEBHOOK_IDENTIFIER=
WHMCS_WEBHOOK_SECRET=
WHMCS_STORE_URL=https://store.example.com
WHMCS_STORE_CART_URL=https://store.example.com/cart.php?a=add&pid=1
```

## Payload

```json
{
  "event": "InvoicePaid",
  "user_id": "11111111-1111-4111-8111-111111111111",
  "credit_delta": 2,
  "tier": "starter",
  "whmcs_client_id": 1001,
  "invoice_id": "8801",
  "service_id": "4401",
  "idempotency_key": "invoice-8801"
}
```

| Field | Required | Notes |
|---|---|---|
| `event` | yes | See events below |
| `user_id` | yes | Supabase auth user UUID. Never inferred from email. |
| `credit_delta` | grant only | Integer 1–1000. Aliases: `credits`, `package_credits`. |
| `tier` | no | Package name (`[A-Za-z0-9_-]{1,32}`) |
| `whmcs_client_id` | no | Integer WHMCS client id |
| `invoice_id` / `service_id` | no | Used for default idempotency |
| `idempotency_key` | no | Retries must send the same key |

`email` may be present for operator debugging on the WHMCS side. The VPS does
not log it and does not use it as identity.

## Events

| WHMCS event | Action | Wallet effect |
|---|---|---|
| `InvoicePaid` | `credit_grant` | Add credits; set tier; `subscription_status=active` |
| `AfterOrderAccept` | `credit_grant` | Same as paid invoice |
| `ServiceSuspensionAudit` | `credit_revoke` | Subtract credits (floor 0); `suspended` |
| `CancellationRequest` | `credit_revoke` | Subtract credits (floor 0); `cancelled` |

Unknown events return `400 invalid_whmcs_event`.

## Success

```json
{
  "ok": true,
  "event": "InvoicePaid",
  "action": "credit_grant",
  "user_id": "11111111-1111-4111-8111-111111111111",
  "credits_available": 2,
  "credits_total": 2,
  "tier": "starter",
  "subscription_status": "active",
  "replayed": false
}
```

A repeated idempotency key returns `200` with `replayed: true` and does not
change the balance again.

## Upgrade button

`GET /billing/config` is public and returns only HTTPS store/cart URLs:

```json
{ "ok": true, "upgrade_url": "https://store.example.com/cart.php?a=add&pid=1", "store_url": "https://store.example.com" }
```

Lovable should use `upgrade_url` (or `VITE_WHMCS_CART_URL`) instead of a
placeholder `/billing` route. The VPS helper is `web/billingConfig.ts`.

## PHP HMAC

```php
$signature = hash_hmac('sha256', $rawJsonBody, getenv('WHMCS_WEBHOOK_SECRET'));
```

WHMCS custom field `supabase_user_id` must be sent as `user_id`.
