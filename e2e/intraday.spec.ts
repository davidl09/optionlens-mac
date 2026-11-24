// Intraday charts (symbol → Chart) against a mocked engine: the range / interval selector, the intraday request, and
// the live candle following streamed quotes (the live channel served frame by frame, e2e/mocks/stream.ts).
import { expect, type Page, test } from "@playwright/test";
import { sse, STATUS } from "./mocks/live";
import { setup } from "./mocks/stream";

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
