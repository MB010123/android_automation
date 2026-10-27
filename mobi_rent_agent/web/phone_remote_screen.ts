/** Shared mapping used by PhoneRemoteScreen.tsx (Lovable copy target). */

export type DisplayRect = {
  left: number;
  top: number;
  width: number;
  height: number;
};

export const SWIPE_THRESHOLD_PX = 12;
export const SETUP_WORKING_FALLBACK_MS = 45_000;
export const SETUP_TIMEOUT_BANNER =
  "Automatic setup timed out after 45 seconds. The phone screen has been unlocked so you can complete your eSIM setup manually.";
export const GALLERY_HANDOFF_PROMPT =
  "Your QR code is saved to the phone's Gallery! On the screen below, tap 'Add eSIM', choose 'Scan QR code' -> 'Gallery', and select the QR image at the top to finish.";
export const SETUP_COMPLETED_BANNER = "eSIM Activated!";

export type SetupUiState = "working" | "awaiting_human_click" | "completed";

export type SetupSurfaceDecision = {
  showSpinner: boolean;
  showPhone: boolean;
  controlsEnabled: boolean;
  timedOut: boolean;
  banner: string | null;
  showRetry: boolean;
};

export function decideSetupSurface(options: {
  setupUiState?: string | null;
  elapsedWorkingMs?: number;
  setupMode?: boolean;
  serverManualFallback?: boolean;
  automatedSetupTimedOut?: boolean;
  humanHandoffUnlocked?: boolean;
}): SetupSurfaceDecision {
  if (!options.setupMode) {
    return {
      showSpinner: false,
      showPhone: true,
      controlsEnabled: true,
      timedOut: false,
      banner: null,
      showRetry: false,
    };
  }
  const state = String(options.setupUiState || "").trim();
  const completed = state === "completed";
  const awaiting =
    state === "awaiting_human_click" || (!!options.humanHandoffUnlocked && !completed);
  if (completed || awaiting) {
    return {
      showSpinner: false,
      showPhone: true,
      controlsEnabled: true,
      timedOut: false,
      banner: null,
      showRetry: false,
    };
  }
  const working = state === "working";
  const timedOut =
    working &&
    !!(
      options.serverManualFallback ||
      options.automatedSetupTimedOut ||
      (options.elapsedWorkingMs || 0) >= SETUP_WORKING_FALLBACK_MS
    );
  if (working && !timedOut) {
    return {
      showSpinner: true,
      showPhone: false,
      controlsEnabled: false,
      timedOut: false,
      banner: null,
      showRetry: false,
    };
  }
  return {
    showSpinner: false,
    showPhone: true,
    controlsEnabled: true,
    timedOut,
    banner: timedOut ? SETUP_TIMEOUT_BANNER : null,
    showRetry: timedOut,
  };
}

export function mapDisplayToDevice(
  clientX: number,
  clientY: number,
  rect: DisplayRect,
  deviceWidth: number,
  deviceHeight: number
): { x: number; y: number } | null {
  if (deviceWidth <= 0 || deviceHeight <= 0 || rect.width <= 0 || rect.height <= 0) {
    return null;
  }
  const dx = clientX - rect.left;
  const dy = clientY - rect.top;
  const scale = Math.min(rect.width / deviceWidth, rect.height / deviceHeight);
  const contentW = deviceWidth * scale;
  const contentH = deviceHeight * scale;
  const padX = (rect.width - contentW) / 2;
  const padY = (rect.height - contentH) / 2;
  if (dx < padX || dy < padY || dx > padX + contentW || dy > padY + contentH) {
    return null;
  }
  const x = Math.round((dx - padX) / scale);
  const y = Math.round((dy - padY) / scale);
  return {
    x: Math.max(0, Math.min(deviceWidth - 1, x)),
    y: Math.max(0, Math.min(deviceHeight - 1, y)),
  };
}

export function classifyGesture(
  start: { x: number; y: number },
  end: { x: number; y: number },
  thresholdPx = SWIPE_THRESHOLD_PX
): "tap" | "swipe" {
  const dist = Math.hypot(end.x - start.x, end.y - start.y);
  return dist >= thresholdPx ? "swipe" : "tap";
}
