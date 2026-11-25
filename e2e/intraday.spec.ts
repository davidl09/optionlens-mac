// Intraday charts (symbol → Chart) against a mocked engine: the range / interval selector, the intraday request, and
// the live candle following streamed quotes (the live channel served frame by frame, e2e/mocks/stream.ts).
import { expect, type Page, test } from "@playwright/test";
import { sse, STATUS } from "./mocks/live";
import { setup } from "./mocks/stream";
import { intradayBars } from "./mocks/symbol";

const summary = (page: Page) => page.locator(".chart-summary");
const radio = (page: Page, group: string, name: string) => page.getByRole("radiogroup", { name: group }).getByRole("radio", { name, exact: true });

test("range and interval selector: intraday ranges offer their intervals and load intraday bars", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  const asked: string[] = [];
  page.on("request", (r) => { if (r.url().includes("/bars?")) asked.push(new URL(r.url()).search); });
  await page.goto("/s/AAPL/chart");
  await expect(summary(page)).toHaveAttribute("data-bars", "252");                   // 1Y daily by default
  await expect(page.getByRole("radiogroup", { name: "Interval" })).toHaveCount(0);
  await radio(page, "Range", "1D").click();
  await expect(page.getByRole("radiogroup", { name: "Interval" }).getByRole("radio")).toHaveText(["1m", "5m", "15m"]);
  await expect(radio(page, "Interval", "1m")).toHaveAttribute("aria-checked", "true");
  await expect(summary(page)).toHaveAttribute("data-bars", String(6 * 60 + 15));      // 09:30..15:44 ET
  await expect(summary(page)).toContainText("Thu 11-19 15:44 ET");
  await radio(page, "Range", "5D").click();
  await expect(page.getByRole("radiogroup", { name: "Interval" }).getByRole("radio")).toHaveText(["5m", "15m", "1h"]);
  await radio(page, "Interval", "15m").click();
  await expect(summary(page)).toHaveAttribute("data-bars", String(4 * 26 + 25));     // four full sessions + today to 15:30
  await page.locator("label.chip", { hasText: "Extended hours" }).click();
  await expect(summary(page)).toHaveAttribute("data-bars", String(4 * 64 + 47));     // 04:00..20:00, today 04:00..15:30
  await expect(page.locator(".legend-note")).toContainText("including pre-market and after-hours");
  await expect(page.locator(".legend-note")).toContainText("SMAs over the loaded bars");
  await expect(page.locator(".legend-note")).toContainText("VWAP computed from the bars (typical price × volume)");
  await expect(page.locator("label.chip", { hasText: "SMA 20 (bars)" })).toBeVisible();
  await radio(page, "Range", "3M").click();
  await expect(page.getByRole("radiogroup", { name: "Interval" }).getByRole("radio")).toHaveText(["1h", "D"]);
  await radio(page, "Interval", "D").click();
  await expect(summary(page)).toHaveAttribute("data-bars", "63");                    // daily 3M (unchanged)
  expect(asked).toContain("?interval=1m&range=1d&extended=0");
  expect(asked).toContain("?interval=15m&range=5d&extended=1");
  expect(asked.at(-1)).toBe("?range=3m");
  expect(live.errors).toEqual([]);
});

test("live candle: streamed quotes update the last bar and roll to a new one at the interval boundary", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  await page.goto("/s/AAPL/chart?range=1d&interval=5m");
  await expect(summary(page)).toHaveAttribute("data-bars", String(6 * 12 + 3));        // 09:30..15:40
  await expect(summary(page)).toContainText("15:40 ET");
  const quote = (price: number, ts: string, volume: number) => sse("status", STATUS.live) + sse("patch", { quotes: { AAPL: { price, volume, ts, source: "questrade" } } });
  live.serve(quote(240.5, "2026-11-19T20:46:00Z", 48_300_000));                       // 15:46 ET: a new 15:45 bar
  await expect(summary(page)).toHaveAttribute("data-bars", String(6 * 12 + 4));
  await expect(summary(page)).toContainText("15:45 ET: open 240.50, high 240.50, low 240.50, close 240.50, volume 0");
  live.serve(quote(241.25, "2026-11-19T20:47:30Z", 48_312_000));                      // same bar: high/close, volume delta
  await expect(summary(page)).toContainText("15:45 ET: open 240.50, high 241.25, low 240.50, close 241.25, volume 12,000");
  live.serve(quote(239.9, "2026-11-19T20:50:05Z", 48_320_000));                       // 15:50: rolls over
  await expect(summary(page)).toHaveAttribute("data-bars", String(6 * 12 + 5));
  await expect(summary(page)).toContainText("15:50 ET: open 239.90, high 239.90, low 239.90, close 239.90, volume 8,000");
  expect(live.errors).toEqual([]);
});

test("live-built minutes: no gap after the page opened, and a reload with lagging candles keeps them", async ({ page }) => {
  // The source's candles end ~15 minutes before now (Questrade, even on a real-time account): the engine answers the
  // stored 1m bars up to 15:29 ET at 15:45 ET; the reload a minute later still lags (to 15:31) and has no live bars.
  await page.clock.install({ time: new Date("2026-11-19T20:45:05Z") });
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  const full = intradayBars("DELL", "1m", "1d", false);
  const asked: string[] = [];
  let until = "2026-11-19T20:30:00.000Z";
  await page.route((u) => u.pathname === "/api/symbol/DELL/bars" && u.searchParams.has("interval"), async (route) => {
    asked.push(until);
    const bars = full.bars.filter((b) => b.ts < until);
    const lag = Math.round((Date.parse("2026-11-19T20:45:05Z") - Date.parse(until)) / 1000) + 60 * (asked.length - 1);
    await route.fulfill({ json: { ...full, bars, lag_s: lag } });
  });
  await page.goto("/s/DELL/chart?range=1d&interval=1m");
  await expect(summary(page)).toHaveAttribute("data-bars", "360");                    // 09:30..15:29
  const quote = (price: number, ts: string, volume: number) => sse("status", STATUS.live) + sse("patch", { quotes: { DELL: { price, volume, ts, source: "questrade" } } });
  live.serve(quote(230.1, "2026-11-19T20:45:10Z", 9_000_000));
  await expect(summary(page)).toHaveAttribute("data-bars", "361");
  live.serve(quote(230.4, "2026-11-19T20:46:10Z", 9_000_400));
  await expect(summary(page)).toHaveAttribute("data-bars", "362");
  live.serve(quote(230.2, "2026-11-19T20:47:10Z", 9_000_900));
  await expect(summary(page)).toHaveAttribute("data-bars", "363");                    // 15:45, 15:46, 15:47: no gap
  await expect(summary(page)).toContainText("15:47 ET: open 230.20");
  await expect(page.locator(".legend-note .live-built")).toHaveText("latest minutes built from live quotes");
  until = "2026-11-19T20:32:00.000Z";
  await page.clock.runFor(61_000);                                                    // the chart reloads its bars
  await expect.poll(() => asked.length).toBe(2);
  await expect(summary(page)).toHaveAttribute("data-bars", String(362 + 3));         // 09:30..15:31 + the live minutes
  await expect(summary(page)).toContainText("15:47 ET: open 230.20");
  live.serve(quote(230.9, "2026-11-19T20:48:05Z", 9_001_500));
  await expect(summary(page)).toHaveAttribute("data-bars", String(362 + 4));
  await expect(summary(page)).toContainText("15:48 ET: open 230.90, high 230.90, low 230.90, close 230.90, volume 600");
  expect(live.errors).toEqual([]);
});

test("a hole before the first live-built bar is marked, not collapsed", async ({ page }) => {
  // Opened at 15:40 ET: Questrade's candles reach 15:29, live-built bars start at 15:40; the engine answers the hole.
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  const full = intradayBars("DELL", "1m", "1d", false);
  const hole = { start: "2026-11-19T20:30:00.000Z", end: "2026-11-19T20:40:00.000Z" };
  const bars = full.bars.filter((b) => b.ts < hole.start || b.ts >= hole.end);
  await page.route((u) => u.pathname === "/api/symbol/DELL/bars" && u.searchParams.has("interval"), (route) =>
    route.fulfill({ json: { ...full, bars, sources: ["questrade", "live"], lag_s: 16 * 60, gaps: [{ ...hole, kind: "lag" }] } }));
  await page.goto("/s/DELL/chart?range=1d&interval=1m");
  await expect(summary(page)).toHaveAttribute("data-bars", String(360 + 5));          // 09:30..15:29, 15:40..15:44
  await expect(summary(page)).toHaveAttribute("data-gaps", "1");
  await expect(page.locator(".legend-note .gap-note")).toHaveText("15:30–15:39 · Questrade candles arrive ~16 min late — filling in");
  await expect(page.locator(".legend-note")).toContainText("overnight and weekend gaps collapsed");
  expect(live.errors).toEqual([]);
});
