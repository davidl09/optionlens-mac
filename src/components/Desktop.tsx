// Desktop-app chrome: waits for the engine sidecar, keeps the API client pointed at it, shows the
// engine-unreachable banner with retry, the IBKR indicator in the header, and follows navigation
// requests from the main process (notification clicks, menu). All of it is inert in a plain browser.
import { ReactNode, useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { BridgeStatus } from "../../electron/shared/api";
import { api, configureClient } from "../api/client";
import { useConnectivity } from "../api/useApi";
import { desktop } from "../desktop";
import { useBridgeStatus, useEngineStatus } from "../state/desktop";
import { Spinner } from "./ui";

export function EngineGate({ children }: { children: ReactNode }) {
  const engine = useEngineStatus();
  const [ready, setReady] = useState(desktop === null);
  const baseUrl = engine?.connection?.baseUrl;
  const token = engine?.connection?.token;
  useEffect(() => {
    if (baseUrl && token) { configureClient({ baseUrl, token }); setReady(true); }
  }, [baseUrl, token]);
  if (ready) return <>{children}</>;
  return (
    <div className="engine-gate" role="status">
      <div className="engine-gate-box">
        <h1>OptionLens</h1>
        {engine?.state === "failed"
          ? <p className="error-text">The engine keeps failing to start: {engine.lastError ?? "see the log"}</p>
          : <Spinner label={engine?.state === "restarting" ? "Restarting the engine…" : "Starting the engine…"} />}
        {engine?.lastError && engine.state !== "failed" && <p className="muted small">{engine.lastError}</p>}
        {engine && <p className="muted small">Data folder: <code>{engine.dataDir}</code></p>}
        {desktop && engine && engine.state !== "starting" && <button className="btn" onClick={() => void desktop?.restartEngine()}>Restart engine</button>}
      </div>
    </div>
  );
}

/** Re-reads the engine connection (desktop) and probes /api/health; success clears the banner. */
async function retryConnection(): Promise<void> {
  if (desktop) {
    const s = await desktop.engine();
    if (s.connection) configureClient(s.connection);
  }
  await api.health().catch(() => undefined);
}

export function ConnectionBanner() {
  const state = useConnectivity();
  const engine = useEngineStatus();
  const [busy, setBusy] = useState(false);
  const retry = useCallback(() => { setBusy(true); void retryConnection().finally(() => setBusy(false)); }, []);
  useEffect(() => {
    if (state === "ok") return undefined;
    const t = window.setInterval(() => void retryConnection(), 10_000);
    return () => window.clearInterval(t);
  }, [state]);
  const engineDown = engine !== null && engine.state !== "running";
  if (state === "ok" && !engineDown) return null;
  const text = engineDown
    ? `The OptionLens engine is ${engine?.state === "failed" ? "failing" : "restarting"}${engine?.lastError ? ` (${engine.lastError})` : ""}. Data shown may be stale.`
    : state === "unauthorized" ? "The engine rejected this app's access token." : "Can't reach the OptionLens engine. Data shown may be stale.";
  return (
    <div className="conn-banner" role="alert">
      <span>{text}</span>
      <button className="btn btn-sm" onClick={retry} disabled={busy}>{busy ? "Retrying…" : "Retry"}</button>
    </div>
  );
}

export function NavigationListener() {
  const navigate = useNavigate();
  useEffect(() => (desktop ? desktop.onNavigate((path) => navigate(path)) : undefined), [navigate]);
  return null;
}

export function bridgeTone(b: BridgeStatus): "pos" | "warn" | "neutral" {
  if (!b.enabled || b.tws !== "connected") return "neutral";
  return b.paused || b.notes.length ? "warn" : "pos";
}

export function bridgeSummary(b: BridgeStatus): string {
  if (!b.enabled) return "IBKR bridge off";
  if (b.tws === "connecting") return "Connecting to IB Gateway / TWS…";
  if (b.tws !== "connected") return b.lastError ?? "Not connected to IB Gateway / TWS";
  return [`${b.endpoint ?? "IBKR"} connected${b.accountType ? ` (${b.accountType})` : ""}`, b.paused, ...b.notes].filter(Boolean).join(" · ");
}

export function BridgeIndicator() {
  const b = useBridgeStatus();
  if (!b) return null;
  return (
    <Link to="/settings" className={`bridge-pill ${bridgeTone(b)}`} title={bridgeSummary(b)} aria-label={`IBKR bridge: ${bridgeSummary(b)}`}>
      <span className="dot" aria-hidden="true" />IBKR{b.current ? <span className="bridge-busy" aria-hidden="true">…</span> : null}
    </Link>
  );
}
