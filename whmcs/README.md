# WHMCS hooks

Copy these files onto the WHMCS host:

- `includes/hooks/mobi_rent_sso.php`
- `includes/hooks/vps_sync.php`

Environment (WHMCS and VPS, never git):

```
MOBI_RENT_SSO_SECRET=
MOBI_RENT_DASHBOARD_URL=https://loanerphones.com
MOBI_RENT_VPS_WEBHOOK_URL=https://api.loanerphones.com
WHMCS_WEBHOOK_IDENTIFIER=
WHMCS_WEBHOOK_SECRET=
```

Product 41 is always an approved phone package. Other products still qualify
when the group or product name contains `phone`, `mobi`, `slot`, `loaner`,
`esim`, or `rent`.
