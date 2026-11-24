import { DependencyList, useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useViewData, type DataRequest } from "../state/view";
import { captureRequests, connection, invalidate } from "./client";

/** Re-renders when the client's reload epoch changes (recovery, new engine connection, IBKR refresh). */
export function useReloadEpoch(): number {
  return useSyncExternalStore(connection.subscribe, connection.epoch);
}

export function useConnectivity() {
  return useSyncExternalStore(connection.subscribe, connection.connectivity);
}

export interface ApiState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  /** Re-run, bypassing the GET cache. */
  refetch: () => void;
  /** Re-run quietly: no loading state, the current data stays until the new data replaces it (live re-runs). */
  reload: () => void;
  /** The engine requests the last run made (what this data is: the current view for agents). */
  requests: DataRequest[];
}

/**
 * Runs `fn` whenever `deps` change; stale responses are discarded.
 * Pass `fn = null` to skip (e.g. until an input is ready).
 * Inside <ViewScope> (the main content) its requests are part of the current view reported for agents.
 */
export function useApi<T>(fn: (() => Promise<T>) | null, deps: DependencyList): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(fn !== null);
  const [nonce, setNonce] = useState(0);
  const [requests, setRequests] = useState<DataRequest[]>([]);
  const quiet = useRef(false);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const epoch = useReloadEpoch();

  useEffect(() => {
    const run = fnRef.current;
    if (!run) { setLoading(false); setRequests((r) => (r.length ? [] : r)); return; }
    let alive = true;
    const silent = quiet.current;
    quiet.current = false;
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    const { value: promise, requests: made } = captureRequests(run);
    setRequests((r) => (JSON.stringify(r) === JSON.stringify(made) ? r : made));
    promise.then(
      (d) => { if (alive) { setData(d); setLoading(false); } },
      (e: unknown) => { if (alive && !silent) { setError(e instanceof Error ? e : new Error(String(e))); setLoading(false); } },
    );
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, epoch]);

  const refetch = useCallback(() => { invalidate(); setNonce((n) => n + 1); }, []);
  const reload = useCallback(() => { quiet.current = true; setNonce((n) => n + 1); }, []);
  useViewData(requests);
  return { data, error, loading, refetch, reload, requests };
}

/** Value that only updates after `ms` of no changes. */
export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}
