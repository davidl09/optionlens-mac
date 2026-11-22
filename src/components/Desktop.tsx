// Desktop-app chrome: waits for the engine sidecar, keeps the API client pointed at it, shows the
// engine-unreachable banner with retry, the data-source pill in the header, and follows navigation
// requests from the main process (notification clicks, menu). All of it is inert in a plain browser.
import { ReactNode, useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { BridgeStatus } from "../../electron/shared/api";
import { api, configureClient } from "../api/client";
import type { QuestradeStatus } from "../api/types";
import { useConnectivity } from "../api/useApi";
import { desktop } from "../desktop";
import { questradeView } from "../lib/sources";
import { bridgePillLabel, populateText } from "../lib/status";
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

/** Connected is good news even with delayed data (a normal state, said in words); only a pause warns. */
export function bridgeTone(b: BridgeStatus): "pos" | "warn" | "neutral" {
  if (!b.enabled || b.tws !== "connected") return "neutral";
  return b.paused ? "warn" : "pos";
}

export function bridgeSummary(b: BridgeStatus): string {
  if (!b.enabled) return "IBKR bridge off";
  if (b.tws === "connecting") return "Connecting to IB Gateway / TWS…";
  if (b.tws !== "connected") return b.lastError ?? "Not connected to IB Gateway / TWS";
  return [`${b.endpoint ?? "IBKR"} connected${b.accountType ? ` (${b.accountType})` : ""}`, b.paused, populateText(b), ...b.notes].filter(Boolean).join(" · ");
}

const QUESTRADE_POLL_MS = 60_000;

/** Questrade's status, re-read every minute (null: not loaded, or Questrade not enabled in this engine). */
export function useQuestradeStatus(): QuestradeStatus | null {
  const [s, setS] = useState<QuestradeStatus | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.questradeStatus().then((x) => { if (alive) setS(x); }, () => { if (alive) setS(null); });
    void load();
    const t = window.setInterval(() => void load(), QUESTRADE_POLL_MS);
    return () => { alive = false; window.clearInterval(t); };
  }, []);
  return s;
}

/**
 * The header's data-source pill: every chain source that is set up, side by side with its state (IBKR via the
 * bridge, Questrade via the engine); "No data source" when neither is. Links to Settings → Data sources.
 */
export function SourcesIndicator() {
  const b = useBridgeStatus();
  const q = useQuestradeStatus();
  const parts: { key: string; tone: string; label: string; title: string; busy?: string }[] = [];
  if (b && b.enabled) parts.push({ key: "ibkr", tone: bridgeTone(b), label: bridgePillLabel(b), title: `IBKR bridge: ${bridgeSummary(b)}`, busy: b.current?.label });
  if (q && q.state !== "disconnected") {
    const v = questradeView(q);
    parts.push({ key: "questrade", tone: v.tone, label: `Questrade ${v.state}`, title: `Questrade: ${[v.state, v.problem, v.dataQuality].filter(Boolean).join(" · ")}` });
  }
  if (!b && !q) return null;
  const title = parts.length ? parts.map((p) => p.title).join("\n") : "No chain source connected: connect IB Gateway or Questrade in Settings → Data sources";
  return (
    <Link to="/settings" className="bridge-pill sources-pill" title={title} aria-label={`Data sources: ${parts.length ? parts.map((p) => p.label).join("; ") : "none connected"}`}>
      {parts.length ? parts.map((p) => (
        <span key={p.key} className={`source-seg ${p.tone}`}>
          <span className="dot" aria-hidden="true" />{p.label}{p.busy ? <span className="bridge-busy" aria-hidden="true" title={p.busy}>…</span> : null}
        </span>
      )) : <span className="source-seg neutral"><span className="dot" aria-hidden="true" />No data source</span>}
    </Link>
  );
}
