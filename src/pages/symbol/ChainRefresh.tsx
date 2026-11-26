// INTEGRATION.md §7 on a symbol page: the on-demand chain refresh from every connected chain source (one round per
// page, shared by the header chip and every options tab) and what a tab shows while the engine has no chain for the
// symbol. The wording is source-neutral; a source is named only when it is the one with a problem and no other source
// delivered a chain.
import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, invalidate, reloadAll } from "../../api/client";
import type { RefreshStatus } from "../../api/types";
import { AsyncErrorView, type ErrorView, Popover, SourceBadge } from "../../components/ui";
import { desktop } from "../../desktop";
import { summarize, type RefreshSummary } from "../../lib/refresh";
import { useBridgeStatus } from "../../state/desktop";

export const POLL_MS = 1500;

interface SymbolRefresh { sym: string; summary: RefreshSummary; retry: () => void }
const Ctx = createContext<SymbolRefresh>({ sym: "", summary: { kind: "none" }, retry: () => undefined });

export const useSymbolRefresh = (): SymbolRefresh => useContext(Ctx);

const delivered = (st: RefreshStatus) =>
  st.sources.filter((s) => s.state === "ok" || s.state === "no_options").map((s) => `${s.name}@${s.at}`);

/**
 * Opening a symbol (or Retry) starts the engine's refresh round — it fetches its own sources (Questrade) — and then asks
 * the bridge (main process) for IBKR; the bridge reports to the same round. The round is polled while a source is
 * pending, and every query on the page reloads each time a source delivered a chain (the first one is shown at once; a
 * better second one replaces it). A refresh IBKR could not run because the bridge was not connected is asked again
 * when it connects.
 */
function useRefreshRound(sym: string): { summary: RefreshSummary; status: RefreshStatus | null; retry: () => void } {
  const [status, setStatus] = useState<RefreshStatus | null>(null);
  const [bridgeAsked, setBridgeAsked] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const seen = new Set<string>();
    setStatus(null);
    setBridgeAsked(false);
    const apply = (st: RefreshStatus) => {
      if (!alive) return;
      setStatus(st);
      const fresh = delivered(st).filter((k) => !seen.has(k));
      fresh.forEach((k) => seen.add(k));
      if (fresh.length) { invalidate(`/api/symbol/${sym}`); reloadAll(); }
      window.clearTimeout(timer);
      if (st.pending) timer = window.setTimeout(poll, POLL_MS);
    };
    const poll = () => { void api.refreshStatus(sym).then(apply, () => { if (alive) timer = window.setTimeout(poll, POLL_MS * 2); }); };
    api.startRefresh(sym).then((st) => {
      apply(st);
      // The bridge reports pending / its outcome to the round itself; read the round again once it answered.
      if (!desktop || !alive) return;
      setBridgeAsked(true);
      const answered = () => { if (alive) { setBridgeAsked(false); poll(); } };
      void desktop.refreshSymbol(sym).then(answered, answered);
    }, () => { if (alive) setStatus(null); });
    // Leaving the page: a refresh still queued behind other bridge work is no longer wanted.
    return () => { alive = false; window.clearTimeout(timer); void desktop?.cancelRefresh(sym).catch(() => undefined); };
  }, [sym, attempt]);

  const bridge = useBridgeStatus();
  // Connected, not paused, and IBKR's contract server reachable (a refresh that waited for it is asked again).
  const runnable = bridge !== null && bridge.tws === "connected" && bridge.paused === null && bridge.contractsAvailable;
  const wasRunnable = useRef(runnable);
  const ibkrWaiting = status?.sources.some((s) => s.name === "ibkr" && s.state === "unavailable") ?? false;
  useEffect(() => {
    const became = runnable && !wasRunnable.current;
    wasRunnable.current = runnable;
    if (became && ibkrWaiting) retry();
  }, [runnable, ibkrWaiting, retry]);

  return { summary: summarize(status, bridgeAsked), status, retry };
}

export function SymbolRefreshProvider({ sym, children }: { sym: string; children: ReactNode }) {
  const { summary, retry } = useRefreshRound(sym);
  return (
    <Ctx.Provider value={{ sym, summary, retry }}>
      <AsyncErrorView.Provider value={noChainView}>{children}</AsyncErrorView.Provider>
    </Ctx.Provider>
  );
}

const fetchingText = (s: RefreshSummary) => (s.kind === "pending" && s.sources.length ? `Fetching chain (${s.sources.join(", ")})…` : "Fetching chain…");

export function RefreshChip() {
  const { summary: s } = useSymbolRefresh();
  switch (s.kind) {
    case "none":
    case "pending":
      return <span className="refresh-chip" role="status"><span className="spinner-dot" />{fetchingText(s)}</span>;
    case "ok":
      return <span className="refresh-chip pos" title="Chain refreshed just now; the best available chain is shown">✓ <SourceBadge source={s.source} delayed={s.delayed} /></span>;
    case "no-options": return <span className="refresh-chip" title="The chain sources list no options for this symbol">No listed options</span>;
    case "no-source":
      return <Link className="refresh-chip" to="/settings" title="Connect IB Gateway or Questrade in Settings → Data sources to fetch chains">No chain source connected</Link>;
    case "waiting":
      if (s.waitingFor === "market-hours") return <span className="refresh-chip" title={s.text}>IBKR: market hours only</span>;
      if (s.waitingFor === "contract-server") return <span className="refresh-chip" title={s.text}>IBKR: waiting for contract server</span>;
      return <span className="refresh-chip" title={s.text}>No chain source available</span>;
    case "failed": return <RefreshFailed text={s.text} details={s.details} />;
  }
}

/** The failed refresh says why (every source's reason) and offers a retry, instead of a bare warning. */
function RefreshFailed({ text, details }: { text: string; details: string[] }) {
  const { sym, retry } = useSymbolRefresh();
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  return (
    <span className="pop-anchor">
      <button className="refresh-chip warn link-btn" aria-expanded={open} onClick={() => setOpen((o) => !o)} title={text}>Refresh failed ▾</button>
      <Popover open={open} onClose={close} align="right">
        <div className="refresh-pop" role="dialog" aria-label="Refresh failed">
          <p><b>Could not refresh {sym}’s option chain.</b></p>
          {details.map((d) => <p className="small" key={d}>{d}</p>)}
          <p className="muted small">Stored data is still shown.</p>
          <button className="btn btn-sm btn-primary" onClick={() => { setOpen(false); retry(); }}>Retry now</button>
        </div>
      </Popover>
    </span>
  );
}

/** The engine answers chain endpoints of a known symbol without a stored chain with these codes. */
const NO_CHAIN_CODES = new Set(["no_chain", "no_options"]);

const noChainView: ErrorView = (error, retry) =>
  error instanceof ApiError && error.code !== null && NO_CHAIN_CODES.has(error.code)
    ? <NoChainNotice noOptions={error.code === "no_options"} onReload={retry} /> : null;

/**
 * In place of an options view while the engine has no chain for the symbol: fetching, how to get it (no source
 * connected / every source failed), or that the symbol lists no options. `noOptions`: the engine knows it lists none.
 */
export function NoChainNotice({ noOptions = false, onReload }: { noOptions?: boolean; onReload?: () => void }) {
  const { sym, summary: s, retry } = useSymbolRefresh();
  if (s.kind === "none" || s.kind === "pending") {
    const via = s.kind === "pending" && s.sources.length ? ` (${s.sources.join(", ")})` : "";
    return (
      <div className="info-box chain-notice" role="status">
        <span className="spinner-dot" /> Fetching {sym}’s option chain{via}… <small>(usually 30–90 s)</small>
      </div>
    );
  }
  if (s.kind === "no-options" || noOptions) return <div className="info-box chain-notice">No listed options for {sym}.</div>;
  if (s.kind === "waiting" && s.waitingFor === "market-hours") {
    return <div className="info-box chain-notice">No option chain stored for {sym} yet. Option quotes update during market hours (no real-time options subscription).</div>;
  }
  if (s.kind === "waiting" && s.waitingFor === "contract-server") {
    return <div className="info-box chain-notice">No option chain stored for {sym} yet. IBKR’s contract server is unavailable; the chain is fetched automatically when it’s back.</div>;
  }
  if (s.kind === "no-source" || s.kind === "waiting") {
    return <div className="info-box chain-notice" title={s.kind === "waiting" ? s.text : undefined}>No option chain stored for {sym} yet. Connect IB Gateway or Questrade (<Link to="/settings">Settings → Data sources</Link>) to fetch it.</div>;
  }
  if (s.kind === "failed") {
    return (
      <div className="info-box chain-notice">
        Could not fetch {sym}’s option chain: {s.text}. <button className="btn btn-sm" onClick={retry}>Try again</button>
      </div>
    );
  }
  return (
    <div className="info-box chain-notice">
      No option chain stored for {sym} yet.{onReload && <> <button className="btn btn-sm" onClick={onReload}>Reload</button></>}
    </div>
  );
}
