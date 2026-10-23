# WHMCS billing webhook → Supabase `user_credits`

WHMCS (`vps_sync.php`) POSTs lifecycle events to the VPS. The VPS verifies the
request, then updates the user's row in Lovable/Supabase **`public.user_credits`**.

Authoritative columns:

| Column | Meaning |
|---|---|
| `user_id` | Supabase auth UUID (primary key) |
| `available_credits` | Spendable credits |
| `total_credits` | Lifetime / package total |
| `subscription_tier` | Package name |
| `whmcs_service_id` | WHMCS service id for the package |

The browser never calls the webhook. Do not put the identifier or HMAC secret
in Lovable, git, or frontend environment files.

## Endpoint

```
POST /api/webhooks/whmcs
Content-Type: application/json
X-WHMCS-Identifier: <WHMCS_WEBHOOK_IDENTIFIER>
X-WHMCS-Signature: <hex HMAC-SHA256 of the raw JSON body>
```

Configure on the VPS only:

```
WHMCS_WEBHOOK_IDENTIFIER=
WHMCS_WEBHOOK_SECRET=
WHMCS_STORE_URL=https://store.example.com
WHMCS_STORE_CART_URL=https://store.example.com/cart.php?a=add&pid=1
LOVABLE_API_URL=https://your-app.example/api/vps
VPS_TO_LOVABLE_API_TOKEN=
```

When `LOVABLE_API_URL` + `VPS_TO_LOVABLE_API_TOKEN` are set, the VPS forwards
verified events to Lovable `POST /billing/whmcs-event`, which **must** upsert
`user_credits` for that `user_id`.

## Payload

```json
{
  "event": "InvoicePaid",
  "user_id": "11111111-1111-4111-8111-111111111111",
  "credit_delta": 2,
  "subscription_tier": "starter",
  "whmcs_client_id": 1001,
  "whmcs_service_id": "4401",
  "invoice_id": "8801",
  "idempotency_key": "invoice-8801"
}
```

Aliases still accepted: `tier` → `subscription_tier`, `service_id` →
`whmcs_service_id`, `credits` / `package_credits` → `credit_delta`.

## Events

| WHMCS event | Action | `user_credits` effect |
|---|---|---|
| `InvoicePaid` | `credit_grant` | `available_credits` += delta; `total_credits` += delta; set `subscription_tier` / `whmcs_service_id` |
| `AfterOrderAccept` | `credit_grant` | Same |
| `ServiceSuspensionAudit` | `credit_revoke` | `available_credits` = max(0, available − delta) |
| `CancellationRequest` | `credit_revoke` | Same; status cancelled |

## Success body (live column names)

```json
{
  "ok": true,
  "table": "user_credits",
  "user_id": "11111111-1111-4111-8111-111111111111",
  "available_credits": 2,
  "total_credits": 2,
  "subscription_tier": "starter",
  "whmcs_service_id": "4401",
  "event": "InvoicePaid",
  "action": "credit_grant",
  "subscription_status": "active",
  "replayed": false
}
```

## Phone activate / terminate

When Lovable is configured, the VPS also:

- Deducts 1 `available_credits` on durable phone reservation (activation)
- Refunds 1 `available_credits` on rental end (termination)

via Lovable `POST /billing/credits/deduct` and `POST /billing/credits/refund`.
Those handlers must update `user_credits` atomically (idempotent by rental id).

## Dashboard / frontend

- Prefer Supabase realtime on `user_credits` filtered by `user_id = auth.uid()`
- Helper: `web/userCredits.ts`
- Or poll `GET /billing/credits` with the user JWT (returns the same columns)
- Upgrade button: `GET /billing/config` → `upgrade_url`

## Dashboard SSO

WHMCS `includes/hooks/mobi_rent_sso.php` redirects paid phone customers to
the dashboard with `payload` (Base64URL JSON) and `sig` (HMAC-SHA256 **hex of
that raw payload string**). The VPS redeems it at:

```
GET|POST /api/public/auth/whmcs-sso
```

`MOBI_RENT_SSO_SECRET` must match on WHMCS and the VPS. The same nonce plus
IP/User-Agent may replay for 10 seconds so React StrictMode does not 401.

## PHP HMAC

```php
$signature = hash_hmac('sha256', $rawJsonBody, getenv('WHMCS_WEBHOOK_SECRET'));
```

WHMCS custom field `supabase_user_id` must be sent as `user_id`.
