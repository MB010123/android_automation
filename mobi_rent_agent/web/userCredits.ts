/**
 * Live Supabase `public.user_credits` helpers for the Lovable dashboard.
 *
 * Columns: available_credits, total_credits, subscription_tier, whmcs_service_id.
 * Prefer realtime on this table so the credit card updates instantly after WHMCS
 * grants or phone activate/terminate mutations.
 *
 * The VPS never uses SUPABASE_SERVICE_ROLE_KEY for this table. Browser reads
 * use the authenticated user JWT; server writes go through Lovable billing APIs.
 */
export const USER_CREDITS_TABLE = "user_credits" as const;

export type UserCreditsRow = {
  user_id: string;
  available_credits: number;
  total_credits: number;
  subscription_tier: string | null;
  whmcs_service_id: string | null;
};

export function normalizeUserCreditsRow(raw: Record<string, unknown> | null | undefined): UserCreditsRow | null {
  if (!raw) return null;
  const userId = String(raw.user_id || "").trim();
  const available = Number(raw.available_credits ?? raw.credits_available);
  const total = Number(raw.total_credits ?? raw.credits_total);
  if (!userId || !Number.isFinite(available) || !Number.isFinite(total)) {
    return null;
  }
  const tier = raw.subscription_tier ?? raw.tier;
  const service = raw.whmcs_service_id ?? raw.service_id;
  return {
    user_id: userId,
    available_credits: Math.max(0, Math.trunc(available)),
    total_credits: Math.max(0, Math.trunc(total)),
    subscription_tier: tier == null || tier === "" ? null : String(tier),
    whmcs_service_id: service == null || service === "" ? null : String(service),
  };
}

/** Select the signed-in user's credits row (RLS must restrict to auth.uid()). */
export function userCreditsSelectQuery(userId: string): {
  table: typeof USER_CREDITS_TABLE;
  filter: { column: "user_id"; value: string };
  columns: string;
} {
  return {
    table: USER_CREDITS_TABLE,
    filter: { column: "user_id", value: userId },
    columns: "user_id,available_credits,total_credits,subscription_tier,whmcs_service_id",
  };
}

/**
 * Example Lovable/Supabase usage:
 *
 *   const q = userCreditsSelectQuery(session.user.id);
 *   const { data } = await supabase.from(q.table).select(q.columns).eq(q.filter.column, q.filter.value).maybeSingle();
 *   const credits = normalizeUserCreditsRow(data);
 *
 *   supabase
 *     .channel(`user_credits:${session.user.id}`)
 *     .on("postgres_changes", { event: "*", schema: "public", table: "user_credits", filter: `user_id=eq.${session.user.id}` },
 *         (payload) => setCredits(normalizeUserCreditsRow(payload.new as Record<string, unknown>)))
 *     .subscribe();
 */
