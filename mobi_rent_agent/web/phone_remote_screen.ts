/** Shared mapping used by PhoneRemoteScreen.tsx (Lovable copy target). */

export type DisplayRect = {
  left: number;
  top: number;
  width: number;
  height: number;
};

export const SWIPE_THRESHOLD_PX = 12;

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
