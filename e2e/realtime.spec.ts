// Live quotes in the UI (INTEGRATION.md §9) against a mocked engine: the live channel (GET /api/live/stream) is served
// frame by frame from this test, everything else from the shared mocks.
import { expect, Page, test } from "@playwright/test";
import { handle } from "./mocks";
import { sse, STATUS } from "./mocks/live";

interface Live {
  /** The body of the next stream request (each stream request waits for one). */
  serve: (frames: string) => void;
  /** The window's subscriptions as posted. */
  subscriptions: { symbols: string[]; option_contracts: string[]; chains: { symbol: string; expiry: string; strike_min: number; strike_max: number }[] }[];
  streams: number;
  errors: string[];
}

async function setup(page: Page, opts: { failStream?: boolean } = {}): Promise<Live> {
  const queue: string[] = [];
  const waiting: ((f: string) => void)[] = [];
  const live: Live = {
    subscriptions: [], streams: 0, errors: [],
    serve: (f) => { const w = waiting.shift(); if (w) w(f); else queue.push(f); },
  };
  page.on("pageerror", (e) => live.errors.push(e.message));
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.pathname === "/api/live/stream") {
      live.streams += 1;
      if (opts.failStream) { await route.fulfill({ status: 503, json: { detail: "down" } }); return; }
      const frames = queue.shift() ?? await new Promise<string>((r) => waiting.push(r));
      await route.fulfill({ status: 200, body: frames, contentType: "text/event-stream" }).catch(() => undefined);
      return;
    }
    let body: unknown = null;
    try { body = req.postDataJSON(); } catch { body = null; }
    if (url.pathname === "/api/live/subscribe") live.subscriptions.push(body as Live["subscriptions"][number]);
    const res = handle(req.method(), url, body);
    if (!res) { await route.fulfill({ status: 501, json: { detail: "not mocked" } }); return; }
    await route.fulfill(res.text !== undefined ? { status: res.status, body: res.text, contentType: res.contentType } : { status: res.status, json: res.body });
  });
  return live;
}

const indicator = (page: Page) => page.locator(".live-pill");
const OSI = "AAPL261120C00230000";

test("header indicator: LIVE · Questrade, DELAYED, MARKET CLOSED", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  await page.goto("/s/AAPL/summary");
  await expect(indicator(page)).toHaveText("LIVE · Questrade");
  await expect(indicator(page)).toHaveAttribute("title", /Questrade snapshots every 2\.0 s/);
  live.serve(sse("status", STATUS.delayed));            // the next stream (after the reconnect) says delayed
  await expect(indicator(page)).toHaveText("DELAYED · Questrade");
  live.serve(sse("status", STATUS.closed));
  await expect(indicator(page)).toHaveText("MARKET CLOSED");
  await expect(indicator(page)).toHaveAttribute("title", /resume Mon 04:00 ET/);
  expect(live.errors).toEqual([]);
});

test("header indicator: NOT CONNECTED when the live channel fails", async ({ page }) => {
  const live = await setup(page, { failStream: true });
  await page.goto("/");
  await expect(indicator(page)).toHaveText("NOT CONNECTED");
  expect(live.streams).toBeGreaterThan(0);
});

test("chain: subscribes the visible expiry and strike window; cells update from the stream and flash", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  await page.goto("/s/AAPL/chain");
  await expect(page.locator("table.chain")).toBeVisible();
  await expect.poll(() => live.subscriptions.at(-1)?.chains?.[0]).toEqual({ symbol: "AAPL", expiry: "2026-11-20", strike_min: 180, strike_max: 280 });
  expect(live.subscriptions.at(-1)?.symbols).toContain("AAPL");
  const bid = page.locator('button.quote-btn[title^="Sell 1 call 230 @"]');
  await expect(bid).toBeVisible();
  const before = await bid.textContent();
  live.serve(sse("status", STATUS.live) + sse("patch", { quotes: {}, options: { [OSI]: { bid: 12.34, ask: 12.6, source: "questrade", ts: "2026-11-20T15:00:00Z" } } }));
  await expect(bid).toHaveText("12.34");
  expect(before).not.toBe("12.34");
  await expect(bid.locator("[data-flashes]")).toHaveAttribute("data-flashes", "1");
  await expect(bid.locator("[data-flashes]")).toHaveAttribute("data-flash-dir", "up");
  const box1 = await page.locator("table.chain").boundingBox();
  live.serve(sse("status", STATUS.live) + sse("patch", { options: { [OSI]: { bid: 12.1 } } }));
  await expect(bid).toHaveText("12.10");
  await expect(bid.locator("[data-flashes]")).toHaveAttribute("data-flashes", "2");
  await expect(bid.locator("[data-flashes]")).toHaveAttribute("data-flash-dir", "down");
  await expect(bid.locator(".live-flash")).toHaveCount(0, { timeout: 2000 });   // the tint goes away again
  const box2 = await page.locator("table.chain").boundingBox();
  expect(box2).toEqual(box1);                            // no layout shift
  // a new expiry re-subscribes
  await page.locator('.pills button[title^="2026-11-27"]').click();
  await expect.poll(() => live.subscriptions.at(-1)?.chains?.[0]?.expiry).toBe("2026-11-27");
  expect(live.errors).toEqual([]);
});

test("header quote follows the live price", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  await page.goto("/s/AAPL/summary");
  await expect(page.locator(".qh-last")).toBeVisible();
  live.serve(sse("status", STATUS.live) + sse("patch", { quotes: { AAPL: { price: 240.5, change: 9.08, change_pct: 0.0392, source: "questrade" } } }));
  await expect(page.locator(".qh-last")).toHaveText("$240.50");
  await expect(page.locator(".qh-last [data-flashes]")).toHaveAttribute("data-flash-dir", "up");
});

test("report: visible rows patch in place without losing the sort", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  await page.goto("/reports/stock-screener");
  await expect(page.getByText("600 rows")).toBeVisible();
  await page.locator("th").filter({ hasText: "Price" }).locator(".th-sort").click();
  expect(new URL(page.url()).searchParams.get("_sort")).toBe("price:desc");
  await expect(page.locator("th[aria-sort=descending]")).toContainText("Price");
  const firstRow = page.locator("table.dt tbody tr").first();
  const sym = (await firstRow.locator("td").first().textContent())!.trim();
  await expect.poll(() => live.subscriptions.at(-1)?.symbols ?? []).toContain(sym);
  const order = await page.locator("table.dt tbody tr td:first-child").allTextContents();
  live.serve(sse("status", STATUS.live) + sse("patch", { quotes: { [sym]: { price: 1.23, change_pct: -0.5, source: "questrade" } } }));
  const priceCell = firstRow.locator("td").nth(2);
  await expect(priceCell).toHaveText("$1.23");
  await expect(priceCell.locator("[data-flashes]")).toHaveAttribute("data-flashes", "1");
  expect(await page.locator("table.dt tbody tr td:first-child").allTextContents()).toEqual(order);   // rows stay put
  expect(new URL(page.url()).searchParams.get("_sort")).toBe("price:desc");
  await expect(page.locator("th[aria-sort=descending]")).toContainText("Price");
  expect(live.errors).toEqual([]);
});

test("a hidden window unsubscribes", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  await page.goto("/s/AAPL/summary");
  await expect.poll(() => live.subscriptions.at(-1)?.symbols ?? []).toContain("AAPL");
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => live.subscriptions.at(-1)).toEqual(expect.objectContaining({ symbols: [], option_contracts: [], chains: [] }));
  await expect(indicator(page)).toHaveCount(0);           // idle: nothing streams while nobody looks
});

test("header indicator names a polling fallback", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", { ...STATUS.live, fallback: "handshake failed" }));
  await page.goto("/");
  await expect(indicator(page)).toHaveText("LIVE · Questrade (polling fallback: handshake failed)");
});
