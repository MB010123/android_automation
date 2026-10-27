/**
 * Lovable copy target for customer "Set up your phone" after QR upload.
 *
 * Hybrid Guided eSIM: as soon as device-status returns
 * setup_ui_state === "awaiting_human_click", hide the spinner, unlock the
 * live canvas, and keep polling until "eSIM Activated!". Canvas taps must
 * never flip the UI back to the working spinner.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { GALLERY_HANDOFF_PROMPT, SETUP_COMPLETED_BANNER } from "./phone_remote_screen";
import { PhoneRemoteScreen, type ControlBody } from "./PhoneRemoteScreen";

type SetupUiState = "working" | "awaiting_human_click" | "completed";

type DeviceStatusBody = {
  setup_ui_state?: string | null;
  setup_ui_prompt?: string | null;
  prompt?: string | null;
  setup_manual_fallback?: boolean;
  esim_activated?: boolean;
  activation_state?: string | null;
};

type Props = {
  rentalId: string;
  connected: boolean;
  frameBlob: Blob | null;
  sendControl: (body: ControlBody) => Promise<{ ok: boolean; error?: string; message?: string } | void>;
  pollDeviceStatus: (signal: AbortSignal) => Promise<DeviceStatusBody>;
  allowedControls?: string[];
};

function readSetupState(body: DeviceStatusBody | null | undefined): SetupUiState {
  if (body?.esim_activated || body?.activation_state === "ACTIVATED" || body?.setup_ui_state === "completed") {
    return "completed";
  }
  if (body?.setup_ui_state === "awaiting_human_click") {
    return "awaiting_human_click";
  }
  return "working";
}

function latchSetupState(previous: SetupUiState, incoming: SetupUiState): SetupUiState {
  if (incoming === "completed") return "completed";
  if (incoming === "awaiting_human_click" || previous === "awaiting_human_click") {
    return "awaiting_human_click";
  }
  return "working";
}

function readPrompt(body: DeviceStatusBody | null | undefined): string {
  const prompt = String(body?.setup_ui_prompt || body?.prompt || "").trim();
  return prompt || GALLERY_HANDOFF_PROMPT;
}

export function AutomaticEsimSetup({
  rentalId,
  connected,
  frameBlob,
  sendControl,
  pollDeviceStatus,
  allowedControls,
}: Props) {
  const [setupUiState, setSetupUiState] = useState<SetupUiState>("working");
  const [setupUiPrompt, setSetupUiPrompt] = useState<string | null>(null);
  const [setupManualFallback, setSetupManualFallback] = useState(false);
  const latchedHandoff = useRef(false);
  const setupUiStateRef = useRef<SetupUiState>("working");

  useEffect(() => {
    const abort = new AbortController();
    let timer: number | undefined;
    const tick = async () => {
      try {
        const body = await pollDeviceStatus(abort.signal);
        if (abort.signal.aborted) return;
        const incoming = readSetupState(body);
        const previous = latchedHandoff.current ? "awaiting_human_click" : setupUiStateRef.current;
        const next = latchSetupState(previous, incoming);
        if (next === "awaiting_human_click") {
          latchedHandoff.current = true;
        }
        if (next === "completed") {
          latchedHandoff.current = false;
        }
        setupUiStateRef.current = next;
        setSetupUiState(next);
        setSetupUiPrompt(readPrompt(body));
        setSetupManualFallback(!!body.setup_manual_fallback);
      } catch {
        if (abort.signal.aborted) return;
      } finally {
        if (!abort.signal.aborted) {
          timer = window.setTimeout(tick, 2000);
        }
      }
    };
    void tick();
    return () => {
      abort.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, [pollDeviceStatus, rentalId]);

  const onRetryAutomaticSetup = useCallback(() => {
    latchedHandoff.current = false;
    setupUiStateRef.current = "working";
    setSetupUiState("working");
    setSetupManualFallback(false);
  }, []);

  const sendWithoutRelock = useCallback(
    (body: ControlBody) => {
      return sendControl(body);
    },
    [sendControl]
  );

  return (
    <PhoneRemoteScreen
      rentalId={rentalId}
      connected={connected}
      frameBlob={frameBlob}
      sendControl={sendWithoutRelock}
      allowedControls={allowedControls}
      setupMode
      setupUiState={setupUiState}
      setupUiPrompt={setupUiPrompt}
      setupManualFallback={setupManualFallback}
      onRetryAutomaticSetup={onRetryAutomaticSetup}
    />
  );
}

export { GALLERY_HANDOFF_PROMPT, SETUP_COMPLETED_BANNER, latchSetupState, readSetupState };
