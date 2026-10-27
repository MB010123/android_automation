/**
 * Drop-in Admin Dashboard cleanup controls for each slot/device card.
 *
 * POST /admin/slots/{slot_id}/clean with the signed-in admin JWT.
 * Admins can clean any slot at any time, including mid-rental. Never disable
 * these buttons because a rental is active, and never tell the operator to
 * release the rental first.
 */
import { useState } from "react";

export const VPS_API_BASE = "https://api.loanerphones.com";
export const KEEP_ESIM_CLEANED_TOAST = "Phone cleaned (eSIM and active rental preserved).";
export const FULL_WIPE_STARTED_TOAST = "Factory reset started. eSIM profiles will be deleted.";

export type CleanSlotResponse = {
  ok?: boolean;
  slot_id?: number;
  preserve_esim?: boolean;
  esim_preserved?: boolean;
  rental_preserved?: boolean;
  rental_id?: string | null;
  status?: string;
  error?: string;
  message?: string;
};

export type AdminSlotCleanupProps = {
  slotId: number;
  getAccessToken: () => Promise<string | null>;
  onSuccess?: (message: string, body: CleanSlotResponse) => void;
  onError?: (message: string) => void;
};

function cleanErrorMessage(body: CleanSlotResponse | null, fallback: string): string {
  const raw = String(body?.message || body?.error || "").trim();
  if (/active rental/i.test(raw) && /release it before cleaning/i.test(raw)) {
    return fallback;
  }
  return raw || fallback;
}

export async function cleanAdminSlot(
  slotId: number,
  preserveEsim: boolean,
  token: string
): Promise<CleanSlotResponse> {
  const response = await fetch(`${VPS_API_BASE}/admin/slots/${encodeURIComponent(String(slotId))}/clean`, {
    method: "POST",
    headers: {
      accept: "application/json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ preserve_esim: preserveEsim }),
  });
  const body = (await response.json().catch(() => null)) as CleanSlotResponse | null;
  if (!response.ok || !body || body.ok === false) {
    throw new Error(cleanErrorMessage(body, "Could not clean this phone"));
  }
  return body;
}

export function AdminSlotCleanup({ slotId, getAccessToken, onSuccess, onError }: AdminSlotCleanupProps) {
  const [busy, setBusy] = useState<"keep" | "wipe" | null>(null);
  const [confirmWipe, setConfirmWipe] = useState(false);

  async function run(preserveEsim: boolean) {
    if (busy) return;
    setBusy(preserveEsim ? "keep" : "wipe");
    try {
      const token = await getAccessToken();
      if (!token) {
        throw new Error("Your session expired. Please sign in again.");
      }
      const body = await cleanAdminSlot(slotId, preserveEsim, token);
      const message = preserveEsim
        ? body.rental_preserved
          ? KEEP_ESIM_CLEANED_TOAST
          : "Phone cleaned (eSIM preserved)."
        : FULL_WIPE_STARTED_TOAST;
      onSuccess?.(message, body);
      setConfirmWipe(false);
    } catch (error) {
      onError?.(error instanceof Error ? error.message : "Could not clean this phone");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="admin-slot-cleanup">
      <button
        type="button"
        disabled={busy !== null}
        onClick={() => void run(true)}
      >
        {busy === "keep" ? <span className="admin-slot-cleanup-spinner" aria-hidden="true" /> : null}
        Clean Phone (Keep eSIM)
      </button>
      <button
        type="button"
        disabled={busy !== null}
        onClick={() => setConfirmWipe(true)}
      >
        {busy === "wipe" ? <span className="admin-slot-cleanup-spinner" aria-hidden="true" /> : null}
        Full Wipe & Delete eSIM
      </button>
      {confirmWipe ? (
        <div role="dialog" aria-modal="true" className="admin-slot-cleanup-dialog">
          <p>This runs a full factory reset and deletes all eSIM profiles on the phone. This cannot be undone.</p>
          <button type="button" disabled={busy !== null} onClick={() => setConfirmWipe(false)}>
            Cancel
          </button>
          <button type="button" disabled={busy !== null} onClick={() => void run(false)}>
            {busy === "wipe" ? <span className="admin-slot-cleanup-spinner" aria-hidden="true" /> : null}
            Wipe phone
          </button>
        </div>
      ) : null}
    </div>
  );
}
