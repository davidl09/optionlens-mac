// Settings → Data sources → Questrade → "Stream live quotes": the one status line under the switch, from GET
// /api/live/status (INTEGRATION.md §9). Pure: the component polls and renders.
import type { LiveMode, LiveStatusReport } from "../api/types";

/** Questrade's own warning (its API documentation): streaming freezes market data in its other platforms. */
export const STREAM_WARNING = "Streaming: tick-by-tick quotes. While OptionLens streams, market data in other Questrade platforms you have open (e.g. Questrade Edge) may freeze — switch to Polling or quit OptionLens to restore them.";

const time = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString() : null);

/** What the live updates are doing now, honestly: streaming connected (what, how fast, last message), connecting, a
 * polling fallback and why, polling, or why nothing is fetched (market closed, nothing on screen, no Questrade). */
export function liveModeStatusText(mode: LiveMode, r: LiveStatusReport | null): string {
  if (!r) return "Live status unavailable.";
  const q = r.questrade;
  if (!q) return "Questrade is not available in this engine.";
  if (r.state === "market_closed") return `Market closed: ${mode === "stream" ? "streams open" : "polling starts"} when it opens.`;
  const bg = r.background ?? 0;
  const followed = `${bg} followed symbol${bg === 1 ? "" : "s"} kept live in the background for their 1-minute bars`;
  if (r.state === "idle") return bg ? `No quotes on screen: ${followed}.` : "No quotes on screen: nothing to update.";
  if (r.state === "not_connected" || r.source === "ibkr") return r.detail ?? "Questrade is not in use for live quotes now.";
  if (mode === "stream" && q.fallback) {
    const retry = time(q.retry_at);
    return `Polling fallback: ${q.fallback}${q.fallback_detail ? ` (${q.fallback_detail})` : ""}.${retry ? ` Streaming is retried at ${retry}.` : ""}`;
  }
  if (mode === "stream" && q.streaming) {
    const open = q.streams.filter((s) => s.open);
    if (!q.streams.length || open.length < q.streams.length) return "Streaming: connecting…";
    const n = (kind: "equity" | "option") => q.streams.reduce((a, s) => a + (s.ids[kind] ?? 0), 0);
    const last = time(q.last_message_at);
    return `Streaming connected: ${n("equity")} symbols, ${n("option")} options · ${q.messages_per_s.toFixed(1)} msg/s · ${last ? `last message ${last}` : "no message yet"}.`;
  }
  return r.interval_s ? `Polling: snapshots every ${r.interval_s.toFixed(1)} s.` : "Polling.";
}
