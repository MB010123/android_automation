<?php
/**
 * WHMCS → VPS billing sync (vps_sync.php).
 *
 * Copy to WHMCS `includes/hooks/vps_sync.php`.
 * Approved phone product is id 41, matching mobi_rent_sso.php. Other packages
 * still qualify when the product/group name contains phone/mobi/slot/etc.
 *
 * Posts HMAC-signed JSON to POST /api/webhooks/whmcs. Secrets stay in env:
 * WHMCS_WEBHOOK_IDENTIFIER, WHMCS_WEBHOOK_SECRET, MOBI_RENT_VPS_WEBHOOK_URL.
 */
if (!defined('WHMCS')) {
    die('This file cannot be accessed directly');
}

use WHMCS\Database\Capsule;

$allowedProductIds = [41];
if (!defined('MOBI_RENT_ALLOWED_PRODUCT_IDS')) {
    define('MOBI_RENT_ALLOWED_PRODUCT_IDS', $allowedProductIds);
}

if (!function_exists('mobirent_isApprovedPhoneProduct')) {
    function mobirent_isApprovedPhoneProduct($packageId): bool
    {
        $packageId = (int) $packageId;
        $allowed = defined('MOBI_RENT_ALLOWED_PRODUCT_IDS') ? MOBI_RENT_ALLOWED_PRODUCT_IDS : [41];
        if ($packageId > 0 && in_array($packageId, $allowed, true)) {
            return true;
        }
        if ($packageId <= 0) {
            return false;
        }
        try {
            $row = Capsule::table('tblproducts')
                ->leftJoin('tblproductgroups', 'tblproducts.gid', '=', 'tblproductgroups.id')
                ->where('tblproducts.id', $packageId)
                ->first([
                    'tblproducts.name as product_name',
                    'tblproductgroups.name as group_name',
                ]);
        } catch (\Throwable $exc) {
            return false;
        }
        if ($row === null) {
            return false;
        }
        $haystack = strtolower(trim((string) ($row->group_name ?? '') . ' ' . (string) ($row->product_name ?? '')));
        foreach (['phone', 'mobi', 'slot', 'loaner', 'esim', 'rent'] as $needle) {
            if ($needle !== '' && str_contains($haystack, $needle)) {
                return true;
            }
        }
        return false;
    }
}

function mobirent_vps_service_is_approved(?object $hosting): bool
{
    if ($hosting === null) {
        return false;
    }
    return mobirent_isApprovedPhoneProduct((int) ($hosting->packageid ?? 0));
}

function mobirent_vps_supabase_user_id(int $clientId): string
{
    try {
        $value = Capsule::table('tblcustomfieldsvalues')
            ->join('tblcustomfields', 'tblcustomfields.id', '=', 'tblcustomfieldsvalues.fieldid')
            ->where('tblcustomfieldsvalues.relid', $clientId)
            ->where('tblcustomfields.fieldname', 'like', '%supabase_user_id%')
            ->value('tblcustomfieldsvalues.value');
        return trim((string) $value);
    } catch (\Throwable $exc) {
        return '';
    }
}

function mobirent_vps_post_event(array $payload): void
{
    $url = rtrim((string) (getenv('MOBI_RENT_VPS_WEBHOOK_URL') ?: getenv('WHMCS_VPS_WEBHOOK_URL') ?: ''), '/');
    $identifier = (string) (getenv('WHMCS_WEBHOOK_IDENTIFIER') ?: '');
    $secret = (string) (getenv('WHMCS_WEBHOOK_SECRET') ?: '');
    if ($url === '' || $identifier === '' || $secret === '') {
        return;
    }
    if (!str_ends_with($url, '/api/webhooks/whmcs')) {
        $url .= '/api/webhooks/whmcs';
    }
    $body = json_encode($payload, JSON_UNESCAPED_SLASHES);
    if (!is_string($body) || $body === '') {
        return;
    }
    $signature = hash_hmac('sha256', $body, $secret);
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => [
            'Content-Type: application/json',
            'X-WHMCS-Identifier: ' . $identifier,
            'X-WHMCS-Signature: ' . $signature,
        ],
        CURLOPT_POSTFIELDS => $body,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 15,
    ]);
    curl_exec($ch);
    curl_close($ch);
}

function mobirent_vps_hosting_for_invoice(int $invoiceId): ?object
{
    try {
        $relid = Capsule::table('tblinvoiceitems')
            ->where('invoiceid', $invoiceId)
            ->where('type', 'Hosting')
            ->value('relid');
        if (!$relid) {
            return null;
        }
        return Capsule::table('tblhosting')->where('id', (int) $relid)->first();
    } catch (\Throwable $exc) {
        return null;
    }
}

function mobirent_vps_emit(string $event, object $hosting, int $invoiceId = 0): void
{
    if (!mobirent_vps_service_is_approved($hosting)) {
        return;
    }
    $clientId = (int) ($hosting->userid ?? 0);
    $userId = mobirent_vps_supabase_user_id($clientId);
    if ($userId === '') {
        return;
    }
    $delta = 1;
    mobirent_vps_post_event([
        'event' => $event,
        'user_id' => $userId,
        'credit_delta' => $delta,
        'subscription_tier' => 'phone',
        'whmcs_client_id' => $clientId,
        'whmcs_service_id' => (string) ($hosting->id ?? ''),
        'invoice_id' => $invoiceId ? (string) $invoiceId : null,
        'idempotency_key' => strtolower($event) . '-' . ($hosting->id ?? '0') . '-' . $invoiceId,
    ]);
}

add_hook('InvoicePaid', 1, function ($vars) {
    $invoiceId = (int) ($vars['invoiceid'] ?? 0);
    $hosting = mobirent_vps_hosting_for_invoice($invoiceId);
    if ($hosting !== null) {
        mobirent_vps_emit('InvoicePaid', $hosting, $invoiceId);
    }
});

add_hook('AfterOrderAccept', 1, function ($vars) {
    $orderId = (int) ($vars['orderid'] ?? 0);
    try {
        $rows = Capsule::table('tblhosting')->where('orderid', $orderId)->get();
    } catch (\Throwable $exc) {
        return;
    }
    foreach ($rows as $hosting) {
        mobirent_vps_emit('AfterOrderAccept', $hosting);
    }
});

add_hook('CancellationRequest', 1, function ($vars) {
    $serviceId = (int) ($vars['relid'] ?? $vars['serviceid'] ?? 0);
    try {
        $hosting = Capsule::table('tblhosting')->where('id', $serviceId)->first();
    } catch (\Throwable $exc) {
        return;
    }
    if ($hosting !== null) {
        mobirent_vps_emit('CancellationRequest', $hosting);
    }
});
