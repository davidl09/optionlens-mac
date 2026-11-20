import { DependencyList, useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { connection, invalidate } from "./client";

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
}

/**
 * Runs `fn` whenever `deps` change; stale responses are discarded.
 * Pass `fn = null` to skip (e.g. until an input is ready).
 */
export function useApi<T>(fn: (() => Promise<T>) | null, deps: DependencyList): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(fn !== null);
  const [nonce, setNonce] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const epoch = useReloadEpoch();

  useEffect(() => {
    const run = fnRef.current;
    if (!run) { setLoading(false); return; }
    let alive = true;
    setLoading(true);
    setError(null);
    run().then(
      (d) => { if (alive) { setData(d); setLoading(false); } },
      (e: unknown) => { if (alive) { setError(e instanceof Error ? e : new Error(String(e))); setLoading(false); } },
    );
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, epoch]);

  const refetch = useCallback(() => { invalidate(); setNonce((n) => n + 1); }, []);
  return { data, error, loading, refetch };
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
