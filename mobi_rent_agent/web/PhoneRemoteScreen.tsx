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
  | { action: "rotate"; orientation: "portrait" | "landscape" };

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
};

const READY_NAV = ["home", "recents", "notification_shade", "quick_settings"] as const;

function rectOf(el: HTMLElement): DisplayRect {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}

export function PhoneRemoteScreen({
  connected,
  frameBlob,
  sendControl,
  disabled,
  allowedControls,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const hitRef = useRef<HTMLDivElement | null>(null);
  const pressRef = useRef<{
    display: { x: number; y: number };
    device: { x: number; y: number };
  } | null>(null);
  const sizeRef = useRef({ w: 0, h: 0 });
  const [ready, setReady] = useState(false);
  const [dot, setDot] = useState<{ x: number; y: number } | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const streamReady = sizeRef.current.w > 0 && sizeRef.current.h > 0;
  const canControl = !!connected && streamReady && !disabled;
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
  ]);

  useEffect(() => {
    if (!frameBlob || !canvasRef.current) return;
    let cancelled = false;
    createImageBitmap(frameBlob).then((bmp) => {
      if (cancelled || !canvasRef.current) {
        bmp.close();
        return;
      }
      const canvas = canvasRef.current;
      if (canvas.width !== bmp.width) canvas.width = bmp.width;
      if (canvas.height !== bmp.height) canvas.height = bmp.height;
      canvas.getContext("2d")?.drawImage(bmp, 0, 0);
      sizeRef.current = { w: bmp.width, h: bmp.height };
      setReady(true);
      bmp.close();
    });
    return () => {
      cancelled = true;
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

  return (
    <div className="phone-remote-wrap">
      <div className="phone-remote-surface" data-ready={canControl ? "true" : "false"}>
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
        {!ready ? (
          <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
            Connecting to your phone…
          </div>
        ) : null}
      </div>
      <div className="phone-remote-toolbar">
        <button type="button" disabled={!canControl || !allowed.has("back")} onClick={() => void dispatch({ action: "back" })}>
          Back
        </button>
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

export { mapDisplayToDevice, classifyGesture };
export type { ControlBody, ControlResult, DisplayRect };
