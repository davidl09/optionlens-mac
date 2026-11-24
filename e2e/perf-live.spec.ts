// Renderer cost of live updates (opt-in: LIVE_PERF=1 npx playwright test e2e/perf-live.spec.ts --trace off).
// A local SSE server stands in for the engine's live channel (reached through vite preview's /api proxy on :8473)
// and patches 300 visible chain contracts (bid, ask, mid, last, volume, IV) 4 times a second — the most the engine
// sends per key — for MEASURE_MS. Reports main-thread busy time (CDP TaskDuration), script time, frame intervals
// and long tasks.
import { expect, test } from "@playwright/test";
import { createServer, type Server } from "node:http";
import type { Chain, OptionRow } from "../src/api/types";
import { handle } from "./mocks";

test.skip(!process.env.LIVE_PERF, "set LIVE_PERF=1 to measure");
test.setTimeout(120_000);

const MEASURE_MS = 10_000;
/** Patch period: 250 ms = the engine's coalescing ceiling (every contract changing 4x/s); LIVE_PERF_MS=3200 = one
 * Questrade poll round of 300 contracts (4 calls) at the default cadence. */
const PERIOD_MS = Number(process.env.LIVE_PERF_MS ?? 250);
const STRIKES = Array.from({ length: 150 }, (_, i) => 100 + i);
const EXP = "2026-11-20";
const osi = (r: "C" | "P", k: number) => `AAPL261120${r}${String(k * 1000).padStart(8, "0")}`;

function chain(): Chain {
  const row = (right: "C" | "P", strike: number): OptionRow => ({
    osi: osi(right, strike), expiry: EXP, strike, right, bid: 1, ask: 1.1, mid: 1.05, last: 1.05, change: 0, volume: 10, oi: 100, iv: 0.3,
    delta: 0.5, gamma: 0.01, theta: -0.01, vega: 0.1, rho: null, theo: null, itm: right === "C" ? strike < 175 : strike > 175,
  });
  const base = handle("GET", new URL("http://x/api/symbol/AAPL/chain"), null)!.body as Chain;
  return { ...base, expiry: EXP, strikes: STRIKES, calls: STRIKES.map((k) => row("C", k)), puts: STRIKES.map((k) => row("P", k)) };
}

function sseServer(): Promise<Server> {
  const server = createServer((req, res) => {
    if (!req.url?.startsWith("/api/live/stream")) { res.writeHead(404).end(); return; }
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
    res.write(`event: status\ndata: ${JSON.stringify({ state: "live", source: "questrade", mode: "poll", interval_s: 3.2 })}\n\n`);
    let n = 0;
    const t = setInterval(() => {
      n += 1;
      const options: Record<string, Record<string, number>> = {};
      for (const k of STRIKES) for (const r of ["C", "P"] as const) {
        const bid = Math.round((1 + Math.random()) * 100) / 100;
        options[osi(r, k)] = { bid, ask: bid + 0.1, mid: bid + 0.05, last: bid + 0.03, volume: 10 + n, iv: 0.3 + Math.random() / 100 };
      }
      res.write(`event: patch\ndata: ${JSON.stringify({ options })}\n\n`);
    }, PERIOD_MS);
    req.on("close", () => clearInterval(t));
  });
  return new Promise((r) => server.listen(8473, "127.0.0.1", () => r(server)));
}

test("300 visible contracts patched 4x/s", async ({ page }) => {
  const server = await sseServer();
  try {
    await page.route("**/api/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/live/stream") { await route.continue(); return; }
      if (url.pathname === "/api/symbol/AAPL/chain") { await route.fulfill({ json: chain() }); return; }
      let body: unknown = null;
      try { body = route.request().postDataJSON(); } catch { body = null; }
      const res = handle(route.request().method(), url, body);
      if (!res) { await route.fulfill({ status: 501, json: {} }); return; }
      await route.fulfill(res.text !== undefined ? { status: res.status, body: res.text, contentType: res.contentType } : { status: res.status, json: res.body });
    });
    await page.goto("/s/AAPL/chain");
    await expect(page.locator("table.chain")).toBeVisible();
    await page.getByRole("radiogroup", { name: "Strikes" }).getByRole("radio", { name: "All" }).click();
    await expect(page.locator("table.chain tbody tr")).toHaveCount(150 + 1);   // + the spot line
    await expect(page.locator(".live-pill")).toHaveText("LIVE · Questrade");
    if (process.env.LIVE_PERF_NOFLASH) await page.addStyleTag({ content: ".live-flash { animation: none !important; }" });
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Performance.enable");
    const metric = async () => Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((m) => [m.name, m.value]));
    await page.evaluate(() => {
      const w = window as unknown as { __frames: number[]; __long: number[] };
      w.__frames = []; w.__long = [];
      let last = performance.now();
      const loop = (t: number) => { w.__frames.push(t - last); last = t; requestAnimationFrame(loop); };
      requestAnimationFrame(loop);
      new PerformanceObserver((l) => { for (const e of l.getEntries()) w.__long.push(e.duration); }).observe({ entryTypes: ["longtask"] });
    });
    const m0 = await metric();
    await page.waitForTimeout(MEASURE_MS);
    const m1 = await metric();
    const { frames, long } = await page.evaluate(() => { const w = window as unknown as { __frames: number[]; __long: number[] }; return { frames: w.__frames.slice(1), long: w.__long }; });
    const sorted = [...frames].sort((a, b) => a - b);
    const pct = (p: number) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))];
    const wall = (m1.Timestamp - m0.Timestamp);
    const result = {
      busyPct: Math.round(((m1.TaskDuration - m0.TaskDuration) / wall) * 1000) / 10,
      scriptPct: Math.round(((m1.ScriptDuration - m0.ScriptDuration) / wall) * 1000) / 10,
      layoutPct: Math.round(((m1.LayoutDuration - m0.LayoutDuration) / wall) * 1000) / 10,
      stylePct: Math.round(((m1.RecalcStyleDuration - m0.RecalcStyleDuration) / wall) * 1000) / 10, periodMs: PERIOD_MS,
      frames: frames.length, frameP50ms: Math.round(pct(0.5) * 10) / 10, frameP95ms: Math.round(pct(0.95) * 10) / 10, frameMaxMs: Math.round(sorted[sorted.length - 1]),
      longTasks: long.length, longestTaskMs: Math.round(Math.max(0, ...long)),
    };
    console.log(`LIVE_PERF ${JSON.stringify(result)}`);
    expect(result.longestTaskMs).toBeLessThan(250);
  } finally {
    server.close();
  }
});
