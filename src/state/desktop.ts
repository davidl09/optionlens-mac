// Live engine / bridge / update status from the desktop app (null in a plain browser).
import { useEffect, useState } from "react";
import type { BridgeStatus, EngineStatus, UpdateStatus } from "../../electron/shared/api";
import { desktop } from "../desktop";

function useDesktopStatus<T>(initial: (() => Promise<T>) | null, subscribe: ((cb: (v: T) => void) => () => void) | null): T | null {
  const [v, setV] = useState<T | null>(null);
  useEffect(() => {
    if (!initial || !subscribe) return undefined;
    let alive = true;
    initial().then((x) => { if (alive) setV(x); }, () => undefined);
    const off = subscribe((x) => { if (alive) setV(x); });
    return () => { alive = false; off(); };
  }, [initial, subscribe]);
  return v;
}

export function useEngineStatus(): EngineStatus | null {
  return useDesktopStatus(desktop ? desktop.engine : null, desktop ? desktop.onEngineStatus : null);
}

export function useBridgeStatus(): BridgeStatus | null {
  return useDesktopStatus(desktop ? desktop.bridgeStatus : null, desktop ? desktop.onBridgeStatus : null);
}

export function useUpdateStatus(): UpdateStatus | null {
  return useDesktopStatus(desktop ? desktop.updateStatus : null, desktop ? desktop.onUpdateStatus : null);
}
