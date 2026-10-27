/**
 * Drop-in replacement for the SlotRent "Phone setup" live screen.
 *
 * Root cause of dead clicks: MJPEG frames were applied by replacing the
 * image element source on every JPEG. That cancels pointer capture and often
 * reports naturalWidth === 0 during the click, so nothing is POSTed.
 *
 * This paints frames onto a <canvas> and captures Pointer Events on a
 * stable overlay. Control still goes to:
 *   POST /rentals/{rentalId}/remote-access/control
 * Allowlist: tap | swipe | type | back | home | recents | notification_shade |
 * quick_settings | rotate.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  mapDisplayToDevice,
  classifyGesture,
  decideSetupSurface,
  GALLERY_HANDOFF_PROMPT,
  SETUP_COMPLETED_BANNER,
  type DisplayRect,
} from "./phone_remote_screen";

type ControlBody =
  | { action: "tap"; x: number; y: number }
  | { action: "swipe"; x: number; y: number; x2: number; y2: number }
  | { action: "type"; text: string }
  | { action: "back" }
  | { action: "home" }
  | { action: "recents" }
  | { action: "notification_shade" }
  | { action: "quick_settings" }
  | { action: "rotate"; orientation: "portrait" | "landscape" }
  | { action: "settings" };

type ControlResult = { ok: boolean; error?: string; message?: string };

type Props = {
  rentalId: string;
  connected: boolean;
  /** JPEG blobs from the existing authenticated ReadableStream parser. */
  frameBlob: Blob | null;
  sendControl: (body: ControlBody) => Promise<ControlResult | void> | ControlResult | void;
  disabled?: boolean;
  /** From session JSON. Full normal Android actions from session start. */
  allowedControls?: string[];
  setupMode?: boolean;
  /** From GET/POST device-status: working | awaiting_human_click | completed. */
  setupUiState?: "working" | "awaiting_human_click" | "completed" | null;
  setupUiPrompt?: string | null;
  /** Server flipped this after 45s of working or a farm walk timeout. */
  setupManualFallback?: boolean;
  automatedSetupTimedOut?: boolean;
  onRetryAutomaticSetup?: () => void;
  /** Admin live view. Does not change customer rental restrictions. */
  adminMode?: boolean;
  customerSessionWarning?: string | null;
};

const READY_NAV = ["home", "recents", "notification_shade", "quick_settings"] as const;

function rectOf(el: HTMLElement): DisplayRect {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}

function jpegBlob(blob: Blob): Promise<Blob> {
  return blob.arrayBuffer().then((buffer) => {
    const bytes = new Uint8Array(buffer);
    let start = 0;
    for (let i = 0; i < bytes.length - 1; i += 1) {
      if (bytes[i] === 0xff && bytes[i + 1] === 0xd8) {
        start = i;
        break;
      }
    }
    let end = bytes.length;
    for (let i = bytes.length - 2; i > start; i -= 1) {
      if (bytes[i] === 0xff && bytes[i + 1] === 0xd9) {
        end = i + 2;
        break;
      }
    }
    if (start === 0 && end === bytes.length) return blob;
    return new Blob([bytes.subarray(start, end)], { type: "image/jpeg" });
  });
}

function decodeWithImage(blob: Blob): Promise<ImageBitmap> {
  return new Promise((resolve, reject) => {
    const decoded = new Image();
    const url = URL.createObjectURL(blob);
    decoded.onload = () => {
      const width = decoded.naturalWidth;
      const height = decoded.naturalHeight;
      URL.revokeObjectURL(url);
      if (width <= 0 || height <= 0) {
        reject(new Error("frame_decode_failed"));
        return;
      }
      createImageBitmap(decoded).then(resolve, reject);
    };
    decoded.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("frame_decode_failed"));
    };
    decoded.src = url;
  });
}

function decodeFrame(blob: Blob): Promise<ImageBitmap> {
  return jpegBlob(blob).then((clean) =>
    createImageBitmap(clean).catch(() => decodeWithImage(clean).catch(() => decodeWithImage(blob)))
  );
}

export function PhoneRemoteScreen({
  connected,
  frameBlob,
  sendControl,
  disabled,
  allowedControls,
  setupMode,
  setupUiState,
  setupUiPrompt,
  setupManualFallback,
  automatedSetupTimedOut,
  onRetryAutomaticSetup,
  adminMode,
  customerSessionWarning,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const hitRef = useRef<HTMLDivElement | null>(null);
  const pressRef = useRef<{
    display: { x: number; y: number };
    device: { x: number; y: number };
  } | null>(null);
  const sizeRef = useRef({ w: 0, h: 0 });
  const workingStartedRef = useRef<number | null>(null);
  const [ready, setReady] = useState(false);
  const [dot, setDot] = useState<{ x: number; y: number } | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [elapsedWorkingMs, setElapsedWorkingMs] = useState(0);
  const [retryNonce, setRetryNonce] = useState(0);
  const [ignoreServerFallback, setIgnoreServerFallback] = useState(false);
  const [humanHandoffUnlocked, setHumanHandoffUnlocked] = useState(false);

  useEffect(() => {
    if (!setupMode) {
      setHumanHandoffUnlocked(false);
      return;
    }
    if (setupUiState === "awaiting_human_click") {
      setHumanHandoffUnlocked(true);
    }
    if (setupUiState === "completed") {
      setHumanHandoffUnlocked(false);
    }
  }, [setupMode, setupUiState]);

  useEffect(() => {
    if (!setupMode || setupUiState !== "working" || humanHandoffUnlocked) {
      workingStartedRef.current = null;
      setElapsedWorkingMs(0);
      if (setupUiState !== "working" || humanHandoffUnlocked) {
        setIgnoreServerFallback(false);
      }
      return;
    }
    if (workingStartedRef.current == null) {
      workingStartedRef.current = Date.now();
    }
    const tick = () => {
      const started = workingStartedRef.current;
      if (started == null) return;
      setElapsedWorkingMs(Date.now() - started);
    };
    tick();
    const id = window.setInterval(tick, 250);
    return () => window.clearInterval(id);
  }, [setupMode, setupUiState, retryNonce, humanHandoffUnlocked]);

  const surface = decideSetupSurface({
    setupUiState,
    elapsedWorkingMs,
    setupMode: !!setupMode,
    serverManualFallback: !!setupManualFallback && !ignoreServerFallback,
    automatedSetupTimedOut: !!automatedSetupTimedOut && !ignoreServerFallback,
    humanHandoffUnlocked,
  });
  const setupComplete = setupMode && setupUiState === "completed";
  const handoffBanner =
    setupMode &&
    !setupComplete &&
    !surface.timedOut &&
    (setupUiState === "awaiting_human_click" || humanHandoffUnlocked)
      ? (setupUiPrompt && setupUiPrompt.trim()) || GALLERY_HANDOFF_PROMPT
      : null;

  const streamReady = sizeRef.current.w > 0 && sizeRef.current.h > 0;
  const canControl = !!connected && streamReady && !disabled && surface.controlsEnabled;
  const allowed = new Set(allowedControls || [
    "tap",
    "swipe",
    "type",
    "back",
    "home",
    "recents",
    "notification_shade",
    "quick_settings",
    "rotate",
    ...(adminMode ? ["settings"] : []),
  ]);

  useEffect(() => {
    if (!frameBlob) return;
    let cancelled = false;
    const paint = (canvas: HTMLCanvasElement) =>
      decodeFrame(frameBlob)
        .then((bmp) => {
          if (cancelled || canvasRef.current !== canvas) {
            bmp.close();
            return;
          }
          if (canvas.width !== bmp.width) canvas.width = bmp.width;
          if (canvas.height !== bmp.height) canvas.height = bmp.height;
          canvas.getContext("2d")?.drawImage(bmp, 0, 0);
          sizeRef.current = { w: bmp.width, h: bmp.height };
          setReady(true);
          bmp.close();
        })
        .catch(() => undefined);
    const canvas = canvasRef.current;
    if (canvas) {
      void paint(canvas);
      return () => {
        cancelled = true;
      };
    }
    const retry = window.requestAnimationFrame(() => {
      const late = canvasRef.current;
      if (late) void paint(late);
    });
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(retry);
    };
  }, [frameBlob]);

  const dispatch = useCallback(
    async (body: ControlBody) => {
      setStatus(null);
      try {
        const result = await sendControl(body);
        if (result && result.ok === false) {
          setStatus(result.message || result.error || "Control failed");
        }
      } catch {
        setStatus("Control failed");
      }
    },
    [sendControl]
  );

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.stopPropagation();
      if (!canControl || !hitRef.current) return;
      hitRef.current.setPointerCapture(event.pointerId);
      const mapped = mapDisplayToDevice(
        event.clientX,
        event.clientY,
        rectOf(hitRef.current),
        sizeRef.current.w,
        sizeRef.current.h
      );
      if (!mapped) return;
      pressRef.current = {
        display: { x: event.clientX, y: event.clientY },
        device: mapped,
      };
      const r = hitRef.current.getBoundingClientRect();
      setDot({ x: event.clientX - r.left, y: event.clientY - r.top });
    },
    [canControl]
  );

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (!pressRef.current || !hitRef.current) return;
    event.preventDefault();
    event.stopPropagation();
    const r = hitRef.current.getBoundingClientRect();
    setDot({ x: event.clientX - r.left, y: event.clientY - r.top });
  }, []);

  const onPointerUp = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.stopPropagation();
      const press = pressRef.current;
      pressRef.current = null;
      setDot(null);
      if (!press || !canControl || !hitRef.current) return;
      const kind = classifyGesture(press.display, { x: event.clientX, y: event.clientY });
      if (kind === "tap") {
        void dispatch({ action: "tap", x: press.device.x, y: press.device.y });
        return;
      }
      const end = mapDisplayToDevice(
        event.clientX,
        event.clientY,
        rectOf(hitRef.current),
        sizeRef.current.w,
        sizeRef.current.h
      );
      if (!end) return;
      void dispatch({
        action: "swipe",
        x: press.device.x,
        y: press.device.y,
        x2: end.x,
        y2: end.y,
      });
    },
    [canControl, dispatch]
  );

  const retryAutomatic = useCallback(() => {
    workingStartedRef.current = Date.now();
    setElapsedWorkingMs(0);
    setIgnoreServerFallback(true);
    setHumanHandoffUnlocked(false);
    setRetryNonce((value) => value + 1);
    onRetryAutomaticSetup?.();
  }, [onRetryAutomaticSetup]);

  return (
    <div className="phone-remote-wrap">
      {setupMode ? <h2 className="phone-setup-title">Set up your phone</h2> : null}
      {adminMode && customerSessionWarning ? (
        <p className="phone-remote-status" role="status">
          {customerSessionWarning}
        </p>
      ) : null}
      {setupComplete ? (
        <div className="phone-setup-banner phone-setup-banner-success" role="status">
          <p>{SETUP_COMPLETED_BANNER}</p>
        </div>
      ) : null}
      {surface.banner ? (
        <div className="phone-setup-banner" role="status">
          <p>{surface.banner}</p>
          {surface.showRetry ? (
            <button type="button" className="phone-setup-retry" onClick={retryAutomatic}>
              Retry automatic setup
            </button>
          ) : null}
        </div>
      ) : null}
      {handoffBanner ? (
        <p className="phone-setup-prompt" role="status">
          {handoffBanner}
        </p>
      ) : null}
      <div
        className="phone-remote-surface"
        data-ready={canControl ? "true" : "false"}
        data-setup-locked={surface.showSpinner ? "true" : "false"}
      >
        <canvas ref={canvasRef} aria-label="Your phone screen" />
        <div
          ref={hitRef}
          className="phone-remote-hit"
          data-ready={canControl ? "true" : "false"}
          role="application"
          aria-label="Remote phone control"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={() => {
            pressRef.current = null;
            setDot(null);
          }}
          onContextMenu={(event) => event.preventDefault()}
          onDragStart={(event) => event.preventDefault()}
        />
        {dot ? <span className="phone-remote-dot" style={{ left: dot.x, top: dot.y }} /> : null}
        {surface.showSpinner ? (
          <div className="phone-setup-spinner" role="status" aria-live="polite">
            <span className="phone-setup-spinner-wheel" aria-hidden="true" />
            <p>Setting up your eSIM…</p>
          </div>
        ) : null}
        {!ready && !surface.showSpinner ? (
          <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
            {adminMode ? "Connecting to the selected phone…" : "Connecting to your phone…"}
          </div>
        ) : null}
      </div>
      <div className="phone-remote-toolbar">
        <button type="button" disabled={!canControl || !allowed.has("back")} onClick={() => void dispatch({ action: "back" })}>
          Back
        </button>
        {adminMode && allowed.has("settings") ? (
          <button
            type="button"
            disabled={!canControl}
            onClick={() => void dispatch({ action: "settings" })}
          >
            Settings
          </button>
        ) : null}
        {READY_NAV.map((action) => (
          <button
            key={action}
            type="button"
            data-action={action}
            disabled={!canControl || !allowed.has(action)}
            onClick={() => void dispatch({ action })}
          >
            {action === "notification_shade"
              ? "Notifications"
              : action === "quick_settings"
                ? "Quick settings"
                : action === "recents"
                  ? "Recents"
                  : "Home"}
          </button>
        ))}
        <button
          type="button"
          disabled={!canControl || !allowed.has("rotate")}
          onClick={() => void dispatch({ action: "rotate", orientation: "portrait" })}
        >
          Portrait
        </button>
        <button
          type="button"
          disabled={!canControl || !allowed.has("rotate")}
          onClick={() => void dispatch({ action: "rotate", orientation: "landscape" })}
        >
          Landscape
        </button>
      </div>
      {status ? (
        <p className="phone-remote-status" role="status">
          {status}
        </p>
      ) : null}
    </div>
  );
}

export {
  mapDisplayToDevice,
  classifyGesture,
  decideSetupSurface,
  GALLERY_HANDOFF_PROMPT,
  SETUP_COMPLETED_BANNER,
};
export type { ControlBody, ControlResult, DisplayRect };
