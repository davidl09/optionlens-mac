// The live-data subsystem's contract for views (INTEGRATION.md §9). A view says what it shows with useLive(...) and
// reads values with useLiveQuote / useLiveOption (or a table cell with useLiveCell); every page uses these same
// hooks and the merge helpers in merge.ts. One store and one engine channel per window.
import { ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { connection as apiConnection, engineFetch } from "../api/client";
import { documentVisibility, LiveConnection } from "./connection";
import { isFlowing, LiveStore, subscriptionKey, type Kind, type LiveFields, type LiveStatus, type LiveSubscription } from "./store";

export { isFlowing } from "./store";
export type { ChainView, LiveFields, LiveStatus, LiveSubscription } from "./store";

export const liveStore = new LiveStore();

let channel: LiveConnection | null = null;

function ensureChannel(): void {
  if (channel || typeof window === "undefined") return;
  channel = new LiveConnection(liveStore, {
    visibility: documentVisibility,
    transport: {
      stream: (clientId, signal) => engineFetch(`/api/live/stream?client_id=${encodeURIComponent(clientId)}`, { headers: { Accept: "text/event-stream" }, signal, cache: "no-store" }),
      subscribe: async (body) => {
        const r = await engineFetch("/api/live/subscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (!r.ok) throw new Error(`subscribe answered ${r.status}`);
        return r.json();
      },
    },
  });
  // A new engine connection (desktop: new port / token, recovery) reopens the stream.
  let epoch = apiConnection.epoch();
  apiConnection.subscribe(() => { if (apiConnection.epoch() !== epoch) { epoch = apiConnection.epoch(); channel?.restart(); } });
  channel.start();
}

/** Keep this view's symbols / contracts / chain bands live while it is mounted (null: nothing). */
export function useLive(sub: LiveSubscription | null): void {
  const key = sub ? subscriptionKey(sub) : null;
  const ref = useRef(sub);
  ref.current = sub;
  useEffect(() => {
    ensureChannel();
    if (key === null || !ref.current) return undefined;
    const id = liveStore.add(ref.current);
    return () => liveStore.remove(id);
  }, [key]);
}

function useKey(kind: Kind, id: string | null | undefined): LiveFields | null {
  const subscribe = useCallback((cb: () => void) => (id ? liveStore.onKey(kind, id, cb) : () => undefined), [kind, id]);
  return useSyncExternalStore(subscribe, () => (id ? liveStore.get(kind, id) : null));
}

/** The live fields of an underlying (null until one arrived). */
export const useLiveQuote = (symbol: string | null | undefined) => useKey("q", symbol ? symbol.toUpperCase() : null);
/** The live fields of an option contract by OSI (null until one arrived). */
export const useLiveOption = (osi: string | null | undefined) => useKey("o", osi);
/** A report cell's live fields (key from merge.liveKeyOf). */
export const useLiveCell = (key: ["q" | "o", string] | null) => useKey(key?.[0] ?? "q", key?.[1] ?? null);

export function useLiveStatus(): LiveStatus {
  useEffect(() => ensureChannel(), []);
  return useSyncExternalStore((cb) => liveStore.onStatus(cb), () => liveStore.status);
}

/** Runs `fn` every `ms` while live values flow and the window is visible (report re-runs). */
export function useLiveInterval(fn: () => void, ms: number): void {
  const status = useLiveStatus();
  const flowing = isFlowing(status);
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!flowing) return undefined;
    const t = window.setInterval(() => { if (document.visibilityState !== "hidden") ref.current(); }, ms);
    return () => window.clearInterval(t);
  }, [flowing, ms]);
}

/** `value`, updated at most once per `ms` (the last value always lands). */
export function useThrottled<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  const last = useRef(0);
  useEffect(() => {
    const wait = Math.max(0, last.current + ms - Date.now());
    const t = window.setTimeout(() => { last.current = Date.now(); setV(value); }, wait);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}

/**
 * Flashing: a changed cell gets a static tint class (no animation: a CSS animation on hundreds of cells repaints the
 * table every frame) and one shared timer takes the tints off again after FLASH_MS, so a patch costs two paints.
 * Background only, so no layout shift; the tint does not move, so it also suits prefers-reduced-motion.
 */
export const FLASH_MS = 600;
const flashing = new Map<HTMLElement, number>();
let flashTimer: number | null = null;

function unflash(): void {
  flashTimer = null;
  const now = performance.now();
  let next = Infinity;
  for (const [el, at] of flashing) {
    if (now - at >= FLASH_MS) { el.classList.remove("live-flash", "live-flash-up", "live-flash-down"); flashing.delete(el); }
    else next = Math.min(next, at + FLASH_MS - now);
  }
  if (next !== Infinity) flashTimer = window.setTimeout(unflash, next);
}

function flash(el: HTMLElement, dir: "up" | "down"): void {
  el.classList.remove(dir === "up" ? "live-flash-down" : "live-flash-up");
  el.classList.add("live-flash", `live-flash-${dir}`);
  el.dataset.flashes = String(Number(el.dataset.flashes ?? 0) + 1);   // how often it flashed, and which way last
  el.dataset.flashDir = dir;
  flashing.set(el, performance.now());
  if (flashTimer === null) flashTimer = window.setTimeout(unflash, FLASH_MS);
}

/** Briefly tints its content when `value` changes while `active` (a live update). */
export function Flash({ value, active, children }: { value: unknown; active: boolean; children: ReactNode }) {
  const ref = useRef<HTMLSpanElement>(null);
  const prev = useRef(value);
  useLayoutEffect(() => {
    const was = prev.current;
    prev.current = value;
    if (!active || Object.is(was, value) || !ref.current) return;
    flash(ref.current, typeof value === "number" && typeof was === "number" && value < was ? "down" : "up");
  }, [value, active]);
  useEffect(() => () => { if (ref.current) flashing.delete(ref.current); }, []);
  return <span ref={ref}>{children}</span>;
}

const LABEL: Record<LiveStatus["state"], string> = {
  live: "LIVE", delayed: "DELAYED", connecting: "CONNECTING", stale: "STALE", market_closed: "MARKET CLOSED", not_connected: "NOT CONNECTED", idle: "",
};
const TONE: Record<LiveStatus["state"], string> = {
  live: "pos", delayed: "warn", connecting: "neutral", stale: "warn", market_closed: "neutral", not_connected: "neg", idle: "neutral",
};
const SOURCE: Record<string, string> = { questrade: "Questrade", ibkr: "IBKR" };

export function liveLabel(s: LiveStatus): string {
  const base = LABEL[s.state];
  const withSource = s.source && (s.state === "live" || s.state === "delayed" || s.state === "stale");
  return withSource ? `${base} · ${SOURCE[s.source!] ?? s.source}` : base;
}

export function liveTitle(s: LiveStatus): string {
  const cadence = s.mode === "poll" && s.interval_s ? `Questrade snapshots every ${s.interval_s.toFixed(1)} s` : s.mode === "stream" ? "Questrade Level 1 stream"
    : s.mode === "snapshots" ? `IBKR snapshots every ${s.interval_s ?? 5} s` : null;
  const scope = s.symbols || s.contracts ? `${s.symbols ?? 0} symbols, ${s.contracts ?? 0} contracts on screen` : null;
  return ["Live quotes", s.detail, cadence, scope].filter(Boolean).join(" · ");
}

/** Header indicator: LIVE · Questrade / DELAYED / MARKET CLOSED / NOT CONNECTED … (nothing while idle). */
export function LiveIndicator() {
  const s = useLiveStatus();
  if (s.state === "idle") return null;
  return (
    <span className={`live-pill ${TONE[s.state]}`} title={liveTitle(s)} role="status" aria-label={`Live quotes: ${liveLabel(s).toLowerCase()}`} data-live-state={s.state}>
      <span className="dot" aria-hidden="true" /><span className="live-text">{liveLabel(s)}</span>
    </span>
  );
}
