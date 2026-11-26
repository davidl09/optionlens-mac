import { describe, expect, it } from "vitest";
import type { LiveStatusReport, QuestradeLiveDiagnostics } from "../api/types";
import { liveLabel, liveTitle } from "../live";
import { liveModeStatusText } from "./liveMode";

const Q: QuestradeLiveDiagnostics = {
  setting: "stream", streaming: true, fallback: null, fallback_detail: null, retry_at: null, messages_per_s: 12.34,
  last_message_at: "2026-11-24T15:00:00Z", drops_recent: 0,
  streams: [
    { ids: { equity: 3, option: 42 }, open: true, port: 18101, opened_at: null, last_message_at: null, messages: 100, connects: 1 },
  ],
};
const R = (over: Partial<LiveStatusReport> = {}, q: Partial<QuestradeLiveDiagnostics> | null = {}): LiveStatusReport => ({
  state: "live", source: "questrade", session: "regular", mode: "stream", interval_s: null, fallback: null, symbols: 3,
  contracts: 42, detail: null, ...over, questrade: q === null ? null : { ...Q, ...q },
});

describe("live mode status line", () => {
  it("says what streaming does, honestly", () => {
    expect(liveModeStatusText("stream", R())).toMatch(/^Streaming connected: 3 symbols, 42 options · 12\.3 msg\/s · last message /);
    expect(liveModeStatusText("stream", R({}, { streams: [{ ...Q.streams[0], open: false }] }))).toBe("Streaming: connecting…");
    expect(liveModeStatusText("stream", R({ mode: "poll", interval_s: 2, fallback: "handshake failed" },
      { streaming: false, fallback: "handshake failed", fallback_detail: "Questrade did not accept the stream token", retry_at: "2026-11-24T15:05:00Z" })))
      .toMatch(/^Polling fallback: handshake failed \(Questrade did not accept the stream token\)\. Streaming is retried at /);
    expect(liveModeStatusText("poll", R({ mode: "poll", interval_s: 2 }, { setting: "poll", streaming: false }))).toBe("Polling: snapshots every 2.0 s.");
    expect(liveModeStatusText("stream", R({ state: "market_closed" }))).toBe("Market closed: streams open when it opens.");
    expect(liveModeStatusText("stream", R({ state: "idle" }))).toBe("No quotes on screen: nothing to update.");
    expect(liveModeStatusText("poll", R({ state: "idle", background: 12 }))).toBe("No quotes on screen: 12 followed symbols kept live in the background for their 1-minute bars.");
    expect(liveModeStatusText("stream", R({}, null))).toBe("Questrade is not available in this engine.");
    expect(liveModeStatusText("stream", null)).toBe("Live status unavailable.");
  });

  it("the header pill stays short; a polling fallback is named in its tooltip", () => {
    const s = { state: "live", source: "questrade", mode: "poll", fallback: "3 drops in 5 min" } as const;
    expect(liveLabel(s)).toBe("LIVE · Questrade");
    expect(liveTitle(s)).toContain("polling fallback: 3 drops in 5 min");
    expect(liveLabel({ state: "live", source: "questrade", mode: "stream", fallback: null })).toBe("LIVE · Questrade");
    expect(liveLabel({ state: "market_closed", source: null, fallback: "handshake failed" })).toBe("MARKET CLOSED");
  });
});
