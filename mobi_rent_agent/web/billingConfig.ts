/**
 * Upgrade / buy-credits destination.
 *
 * Lovable should pass the live WHMCS cart URL from VITE_WHMCS_CART_URL or
 * GET /billing/config (upgrade_url). Do not hard-code secrets here.
 */
export const BILLING_PLACEHOLDER_PATH = "/billing";

export function resolveBillingUpgradeUrl(
  configured: string | null | undefined
): string | null {
  const url = String(configured || "").trim();
  if (!url || url === BILLING_PLACEHOLDER_PATH) {
    return null;
  }
  if (!url.startsWith("https://")) {
    return null;
  }
  return url;
}

export function billingUpgradeHref(
  configured: string | null | undefined
): string {
  return resolveBillingUpgradeUrl(configured) || BILLING_PLACEHOLDER_PATH;
}
