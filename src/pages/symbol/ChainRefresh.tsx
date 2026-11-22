// INTEGRATION.md §7 on a symbol page: the on-demand IBKR chain refresh (one per page, shared by the
// header chip and every options tab) and what a tab shows while the engine has no chain for the symbol.
import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { RefreshResult } from "../../../electron/shared/api";
import { ApiError, invalidate, reloadAll } from "../../api/client";
import { AsyncErrorView, type ErrorView, Popover } from "../../components/ui";
import { desktop } from "../../desktop";
import { useBridgeStatus } from "../../state/desktop";

/** null: no bridge (plain browser); "pending": the bridge is fetching (or queued to fetch) the chain. */
export type RefreshState = RefreshResult | "pending" | null;

interface SymbolRefresh { sym: string; state: RefreshState; retry: () => void }
const Ctx = createContext<SymbolRefresh>({ sym: "", state: null, retry: () => undefined });

export const useSymbolRefresh = (): SymbolRefresh => useContext(Ctx);

/**
 * Opening a symbol asks the bridge (main process) to fetch its chain from IBKR and push it to the engine;
 * once the bridge pushed a chain (or reported that IBKR lists no options) every query on the page reloads.
 * A refresh the bridge could not run because it was not connected is asked again when it connects.
 */
function useIbkrRefresh(sym: string): SymbolRefresh {
  const [state, setState] = useState<RefreshState>(null);
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  useEffect(() => {
    if (!desktop) return undefined;
    let alive = true;
    setState("pending");
    desktop.refreshSymbol(sym).then((r) => {
      if (!alive) return;
      setState(r);
      if (r.status === "refreshed" || r.status === "no-options") { invalidate(`/api/symbol/${sym}`); reloadAll(); }
    }, (e: unknown) => { if (alive) setState({ status: "failed", message: e instanceof Error ? e.message : String(e) }); });
    // Leaving the page: a refresh still queued behind other bridge work is no longer wanted.
    return () => { alive = false; void desktop?.cancelRefresh(sym).catch(() => undefined); };
  }, [sym, attempt]);

  const bridge = useBridgeStatus();
  // Connected, not paused, and IBKR's contract server reachable (a refresh that waited for it is asked again).
  const runnable = bridge !== null && bridge.tws === "connected" && bridge.paused === null && bridge.contractsAvailable;
  const wasRunnable = useRef(runnable);
  useEffect(() => {
    const became = runnable && !wasRunnable.current;
    wasRunnable.current = runnable;
    if (became && state !== "pending" && state?.status === "unavailable") retry();
  }, [runnable, state, retry]);

  return { sym, state, retry };
}

export function SymbolRefreshProvider({ sym, children }: { sym: string; children: ReactNode }) {
  const refresh = useIbkrRefresh(sym);
  return (
    <Ctx.Provider value={refresh}>
      <AsyncErrorView.Provider value={noChainView}>{children}</AsyncErrorView.Provider>
    </Ctx.Provider>
  );
}

export function RefreshChip() {
  const { state } = useSymbolRefresh();
  if (state === null) return null;
  if (state === "pending") return <span className="refresh-chip" role="status"><span className="spinner-dot" />IBKR refresh…</span>;
  switch (state.status) {
    case "refreshed": return <span className="refresh-chip pos" title="Chain fetched from IBKR just now">IBKR ✓</span>;
    case "fresh": return <span className="refresh-chip pos" title="Chain from IBKR is recent">IBKR ✓</span>;
    case "no-options": return <span className="refresh-chip" title="IBKR lists no options for this symbol">IBKR: no options</span>;
    case "unavailable":
      if (state.waitingFor === "market-hours") return <span className="refresh-chip" title={state.message}>IBKR: market hours only</span>;
      if (state.waitingFor === "contract-server") return <span className="refresh-chip" title={state.message}>IBKR: waiting for contract server</span>;
      return <span className="refresh-chip" title={state.message}>IBKR off</span>;
    case "failed": return <RefreshFailed message={state.message} />;
  }
}

/** The failed refresh says why (the bridge's message) and offers a retry, instead of a bare warning. */
function RefreshFailed({ message }: { message: string }) {
  const { sym, retry } = useSymbolRefresh();
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  return (
    <span className="pop-anchor">
      <button className="refresh-chip warn link-btn" aria-expanded={open} onClick={() => setOpen((o) => !o)} title={message}>IBKR refresh failed ▾</button>
      <Popover open={open} onClose={close} align="right">
        <div className="refresh-pop" role="dialog" aria-label="IBKR refresh failed">
          <p><b>IBKR could not refresh {sym}’s option chain.</b></p>
          <p className="small">{message}</p>
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
 * In place of an options view while the engine has no chain for the symbol: fetching (the bridge is on it),
 * how to get it (bridge not connected / refresh failed), or that the symbol lists no options.
 * `noOptions`: the engine knows the symbol lists no options.
 */
export function NoChainNotice({ noOptions = false, onReload }: { noOptions?: boolean; onReload?: () => void }) {
  const { sym, state, retry } = useSymbolRefresh();
  if (state === "pending") {
    return (
      <div className="info-box chain-notice" role="status">
        <span className="spinner-dot" /> Fetching {sym}’s option chain from IBKR… <small>(usually 30–90 s)</small>
      </div>
    );
  }
  if (state?.status === "no-options") return <div className="info-box chain-notice">IBKR lists no options for {sym}.</div>;
  if (noOptions) return <div className="info-box chain-notice">No listed options for {sym}.</div>;
  if (state?.status === "unavailable" && state.waitingFor === "market-hours") {
    return <div className="info-box chain-notice">No option chain stored for {sym} yet. Option quotes update during market hours (no real-time options subscription).</div>;
  }
  if (state?.status === "unavailable" && state.waitingFor === "contract-server") {
    return <div className="info-box chain-notice">No option chain stored for {sym} yet. IBKR’s contract server is unavailable; the chain is fetched automatically when it’s back.</div>;
  }
  if (state?.status === "unavailable") {
    return <div className="info-box chain-notice" title={state.message}>No option chain stored for {sym} yet. Connect IB Gateway to fetch it.</div>;
  }
  if (state?.status === "failed") {
    return (
      <div className="info-box chain-notice">
        IBKR could not fetch {sym}’s option chain: {state.message} <button className="btn btn-sm" onClick={retry}>Try again</button>
      </div>
    );
  }
  return (
    <div className="info-box chain-notice">
      No option chain stored for {sym} yet.{onReload && <> <button className="btn btn-sm" onClick={onReload}>Reload</button></>}
    </div>
  );
}
