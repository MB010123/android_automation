<?php
/**
 * WHMCS → Mobi-Rent dashboard SSO.
 *
 * Copy to WHMCS `includes/hooks/mobi_rent_sso.php`.
 * Set MOBI_RENT_SSO_SECRET and MOBI_RENT_DASHBOARD_URL in the WHMCS environment
 * (never commit the secret). HMAC-SHA256 is over the raw Base64URL payload
 * string, not the decoded JSON.
 */
if (!defined('WHMCS')) {
    die('This file cannot be accessed directly');
}

use WHMCS\Database\Capsule;

if (!defined('MOBI_RENT_ALLOWED_PRODUCT_IDS')) {
    define('MOBI_RENT_ALLOWED_PRODUCT_IDS', [41]);
}

if (!function_exists('mobirent_isApprovedPhoneProduct')) {
    /**
     * Product 41 is always approved. Any other package id still falls through
     * to group/product name substrings (phone, mobi, slot, loaner, esim).
     */
    function mobirent_isApprovedPhoneProduct($packageId): bool
    {
        $packageId = (int) $packageId;
        $allowed = MOBI_RENT_ALLOWED_PRODUCT_IDS;
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

function mobirent_ssoSecret(): string
{
    $secret = (string) (getenv('MOBI_RENT_SSO_SECRET') ?: '');
    if ($secret === '' && defined('MOBI_RENT_SSO_SECRET')) {
        $secret = (string) MOBI_RENT_SSO_SECRET;
    }
    return $secret;
}

function mobirent_dashboardUrl(): string
{
    $url = (string) (getenv('MOBI_RENT_DASHBOARD_URL') ?: '');
    if ($url === '' && defined('MOBI_RENT_DASHBOARD_URL')) {
        $url = (string) MOBI_RENT_DASHBOARD_URL;
    }
    return rtrim($url, '/');
}

function mobirent_base64urlEncode(string $raw): string
{
    return rtrim(strtr(base64_encode($raw), '+/', '-_'), '=');
}

function mobirent_clientHasApprovedPhone(int $clientId): bool
{
    if ($clientId <= 0) {
        return false;
    }
    try {
        $packageIds = Capsule::table('tblhosting')
            ->where('userid', $clientId)
            ->whereIn('domainstatus', ['Active', 'Pending', 'Completed'])
            ->pluck('packageid');
    } catch (\Throwable $exc) {
        return false;
    }
    foreach ($packageIds as $packageId) {
        if (mobirent_isApprovedPhoneProduct($packageId)) {
            return true;
        }
    }
    return false;
}

function mobirent_buildDashboardSsoUrl(int $clientId): ?string
{
    $secret = mobirent_ssoSecret();
    $dashboard = mobirent_dashboardUrl();
    if ($secret === '' || $dashboard === '' || $clientId <= 0) {
        return null;
    }
    $email = '';
    $supabaseUserId = '';
    try {
        $client = Capsule::table('tblclients')->where('id', $clientId)->first(['email']);
        $email = strtolower(trim((string) ($client->email ?? '')));
        $supabaseUserId = (string) Capsule::table('tblcustomfieldsvalues')
            ->join('tblcustomfields', 'tblcustomfields.id', '=', 'tblcustomfieldsvalues.fieldid')
            ->where('tblcustomfieldsvalues.relid', $clientId)
            ->where('tblcustomfields.fieldname', 'like', '%supabase_user_id%')
            ->value('tblcustomfieldsvalues.value');
    } catch (\Throwable $exc) {
        // Payload can still be signed with client_id alone.
    }
    $now = time();
    $payloadData = [
        'client_id' => $clientId,
        'email' => $email,
        'user_id' => trim($supabaseUserId),
        'nonce' => bin2hex(random_bytes(16)),
        'iat' => $now,
        'exp' => $now + 60,
    ];
    $payload = mobirent_base64urlEncode(
        json_encode($payloadData, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE)
    );
    // Sign the raw Base64URL payload string, not the decoded JSON.
    $signature = hash_hmac('sha256', $payload, $secret);
    $finalUrl = $dashboard . '/auth/whmcs-sso?payload=' . rawurlencode($payload) . '&sig=' . rawurlencode($signature);
    if (function_exists('logModuleCall')) {
        logModuleCall(
            'mobi_rent_sso',
            'buildDashboardSsoUrl',
            $payloadData,
            ['url' => $finalUrl, 'sig' => $signature]
        );
    }
    return $finalUrl;
}

function mobirent_redirectNow(string $url): void
{
    if ($url === '') {
        return;
    }
    if (!headers_sent()) {
        header('Cache-Control: no-store, no-cache, must-revalidate');
        header('Location: ' . $url, true, 303);
    } else {
        echo '<script>window.location.replace(' . json_encode($url, JSON_UNESCAPED_SLASHES) . ');</script>';
    }
    // Persist $_SESSION (redirected invoices, uid) before the 303 exits.
    session_write_close();
    exit;
}

function mobirent_paidDashboardRedirect(array $vars): void
{
    $clientId = (int) ($_SESSION['uid'] ?? $vars['client']['id'] ?? $vars['clientsdetails']['id'] ?? 0);
    if ($clientId <= 0 || !mobirent_clientHasApprovedPhone($clientId)) {
        return;
    }
    $invoiceId = (int) (
        $vars['invoiceid']
        ?? $vars['invoiceId']
        ?? $_REQUEST['id']
        ?? $_REQUEST['invoiceid']
        ?? 0
    );
    if ($invoiceId > 0) {
        if (!isset($_SESSION['mobirent_redirected_invoices']) || !is_array($_SESSION['mobirent_redirected_invoices'])) {
            $_SESSION['mobirent_redirected_invoices'] = [];
        }
        if (!empty($_SESSION['mobirent_redirected_invoices'][$invoiceId])) {
            return;
        }
        $_SESSION['mobirent_redirected_invoices'][$invoiceId] = true;
    }
    $url = mobirent_buildDashboardSsoUrl($clientId);
    if ($url) {
        mobirent_redirectNow($url);
    }
}

add_hook('ClientAreaPage', 1, function ($vars) {
    if (!empty($_GET['mobirent_sso'])) {
        $clientId = (int) ($_SESSION['uid'] ?? 0);
        if ($clientId > 0 && mobirent_clientHasApprovedPhone($clientId)) {
            $url = mobirent_buildDashboardSsoUrl($clientId);
            if ($url) {
                mobirent_redirectNow($url);
            }
        }
        return $vars;
    }
    mobirent_paidDashboardRedirect($vars);
    return $vars;
});

add_hook('ShoppingCartCheckoutCompletePage', 1, function ($vars) {
    mobirent_paidDashboardRedirect($vars);
    return $vars;
});

// Footer: static link only. Do not generate an SSO token here — that burned
// a nonce on every page view and raced the paid-invoice redirect.
add_hook('ClientAreaFooterOutput', 1, function ($vars) {
    unset($vars);
    return '<p class="mobirent-sso"><a href="clientarea.php?mobirent_sso=1">Open Mobi-Rent Dashboard</a></p>';
});
