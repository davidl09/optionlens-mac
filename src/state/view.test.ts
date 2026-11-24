// The current view for agents (INTEGRATION.md §10): route facts, and which engine requests a page's data comes from.
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, captureRequests, invalidate } from "../api/client";
import { describeRoute } from "./view";

describe("describeRoute", () => {
  it("reads symbol pages, reports and other routes", () => {
    expect(describeRoute("/s/aapl/chain", "?expiry=2026-12-18")).toEqual({ route: "/s/aapl/chain?expiry=2026-12-18", symbol: "AAPL", tab: "chain", report_id: null });
    expect(describeRoute("/s/SPY", "")).toEqual({ route: "/s/SPY", symbol: "SPY", tab: "summary", report_id: null });
    expect(describeRoute("/s/AAPL/contract/AAPL261218C00350000", "")).toMatchObject({ symbol: "AAPL", tab: "contract" });
    expect(describeRoute("/reports/iv-rankings", "?min_price=5")).toEqual({ route: "/reports/iv-rankings?min_price=5", symbol: null, tab: null, report_id: "iv-rankings" });
    expect(describeRoute("/", "")).toEqual({ route: "/", symbol: null, tab: null, report_id: null });
  });
});

describe("captureRequests", () => {
  afterEach(() => { vi.unstubAllGlobals(); invalidate(); });
  const stubFetch = () => vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));

  it("records the reads a data function starts, cached or not, exactly as sent", async () => {
    stubFetch();
    const first = captureRequests(() => api.chain("aapl", "2026-12-18"));
    await first.value;
    expect(first.requests).toEqual([{ method: "GET", path: "/api/symbol/AAPL/chain?expiry=2026-12-18" }]);
    const cached = captureRequests(() => api.chain("AAPL", "2026-12-18"));      // served from the cache: still the page's data
    expect(cached.requests).toEqual(first.requests);
    await cached.value;
  });

  it("records read-style POSTs with their body; nothing outside a capture", async () => {
    stubFetch();
    const req = { symbol: "AAPL", legs: [{ kind: "stock" as const, qty: 100 }] };
    const c = captureRequests(() => api.calcStrategy(req));
    await c.value;
    expect(c.requests).toEqual([{ method: "POST", path: "/api/calc/strategy", body: req }]);
    const outer = captureRequests(() => { const inner = captureRequests(() => api.status()); return inner.requests; });
    expect(outer.requests).toEqual([]);                                          // nested captures keep their own
    expect(outer.value).toEqual([{ method: "GET", path: "/api/status" }]);
    await api.createWatchlist({ name: "x" });                                     // a mutation: never part of a view
  });
});
