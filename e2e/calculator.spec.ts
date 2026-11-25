// Calculator pricing against a mocked engine with a frame-by-frame live channel: a typed price is the user's (pinned)
// and drives cost / P/L / EV; "Reset to live" follows the live mid again; a live tick moves unedited legs only.
import { expect, type Page, test } from "@playwright/test";
import type { CalcRequest } from "../src/api/types";
import { sse, STATUS } from "./mocks/live";
import { setup } from "./mocks/stream";

const OSI = "AAPL261120C00230000";

const stat = (page: Page, label: string) => page.locator(".stat", { has: page.locator("dt", { hasText: new RegExp(`^${label.replace(/[()]/g, "\\$&")}$`) }) }).locator("dd");
const priceInput = (page: Page, i: number) => page.getByLabel("Entry price").nth(i);
const priceOrigin = (page: Page, i: number) => page.locator(".legs-table tbody tr").nth(i).locator(".leg-origin").first();

/** The calc requests the page sent. */
function calcRequests(page: Page): CalcRequest[] {
  const out: CalcRequest[] = [];
  page.on("request", (r) => { if (r.url().endsWith("/api/calc/strategy") && r.method() === "POST") out.push(r.postDataJSON() as CalcRequest); });
  return out;
}

/** Buy and sell the 230 call of 2026-11-20 from the chain (both legs priced at the clicked ask / bid: pinned). */
async function twoLegs(page: Page) {
  await page.goto("/s/AAPL/chain");
  await page.locator('button.quote-btn[title^="Buy 1 call 230 @"]').click();
  await page.locator('button.quote-btn[title^="Sell 1 call 230 @"]').click();
  await expect(page.getByText("2 legs in calculator")).toBeVisible();
  await page.getByRole("link", { name: "Open calculator →" }).click();
  await expect(page.locator(".legs-table tbody tr")).toHaveCount(2);
  await expect(stat(page, "EV (IV)")).not.toHaveText("");
}

test("a typed price is pinned and drives cost, P/L and EV", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  const sent = calcRequests(page);
  await twoLegs(page);
  await expect(priceOrigin(page, 0)).toHaveAttribute("data-origin", "user");      // the clicked ask: your price
  await page.getByRole("button", { name: "Remove leg" }).nth(1).click();           // one long call
  await expect(page.locator(".legs-table tbody tr")).toHaveCount(1);
  await priceInput(page, 0).fill("4");
  await expect(stat(page, "Net debit")).toHaveText("$400.00");
  const ev4 = await stat(page, "EV (IV)").textContent();
  const maxLoss4 = await stat(page, "Max loss").textContent();
  await priceInput(page, 0).fill("6");
  await expect(stat(page, "Net debit")).toHaveText("$600.00");
  await expect(stat(page, "EV (IV)")).not.toHaveText(ev4!);
  await expect(stat(page, "Max loss")).not.toHaveText(maxLoss4!);
  await expect(priceOrigin(page, 0)).toHaveText("your price");
  const last = sent.at(-1)!;
  expect(last.legs[0]).toMatchObject({ price: 6 });
  expect(last.legs[0].iv).toBeGreaterThan(0);                                      // explicit IV (the stored one)
  expect(last.spot).toBeGreaterThan(0);                                            // and spot
  expect(live.errors).toEqual([]);
});

test("Reset to live follows the live mid; a live tick moves unedited legs only", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  const sent = calcRequests(page);
  await twoLegs(page);
  const pinned = await priceInput(page, 0).inputValue();
  expect(Number(pinned)).toBeGreaterThan(0);
  // the short leg follows live data again
  await page.locator(".legs-table tbody tr").nth(1).getByRole("button", { name: "Reset to live" }).click();
  await expect(priceInput(page, 1)).toHaveValue("");
  await expect(priceOrigin(page, 1)).not.toHaveAttribute("data-origin", "user");
  await expect(page.getByRole("button", { name: "Reset to live" })).toHaveCount(1);   // only the pinned leg has one
  // a live tick of that contract: bid 9.80 / ask 10.20 → mid 10.00
  live.serve(sse("status", STATUS.live) + sse("patch", { options: { [OSI]: { bid: 9.8, ask: 10.2, iv: 0.31, source: "questrade", ts: "2026-11-20T15:00:00Z" } } }));
  await expect(priceInput(page, 1)).toHaveAttribute("placeholder", "10.00");
  await expect(priceOrigin(page, 1)).toHaveAttribute("data-origin", "live");
  await expect(priceInput(page, 0)).toHaveValue(pinned);                           // your price stays
  await expect.poll(() => sent.at(-1)?.legs.map((l) => l.price)).toEqual([Number(pinned), 10]);
  const cost = Number(pinned) * 100 - 1000;
  await expect(stat(page, cost >= 0 ? "Net debit" : "Net credit")).toHaveText(`$${Math.abs(cost).toFixed(2)}`);
  await expect(page.locator(".card-sub .asof")).toContainText("live as of");
  // another tick: the unedited leg moves again, the pinned one does not
  live.serve(sse("status", STATUS.live) + sse("patch", { options: { [OSI]: { bid: 10.8, ask: 11.2 } } }));
  await expect(priceInput(page, 1)).toHaveAttribute("placeholder", "11.00");
  await expect.poll(() => sent.at(-1)?.legs.map((l) => l.price)).toEqual([Number(pinned), 11]);
  await expect(priceInput(page, 0)).toHaveValue(pinned);
  // Reset all: both legs follow live
  await page.getByRole("button", { name: "Reset all" }).click();
  await expect(priceInput(page, 0)).toHaveValue("");
  await expect.poll(() => sent.at(-1)?.legs.map((l) => l.price)).toEqual([11, 11]);
  expect(live.errors).toEqual([]);
});

test("live re-pricing is throttled to ≤ 2 requests per second", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  const times: number[] = [];
  page.on("request", (r) => { if (r.url().endsWith("/api/calc/strategy")) times.push(Date.now()); });
  await twoLegs(page);
  await page.getByRole("button", { name: "Reset all" }).click();
  const frames = Array.from({ length: 20 }, (_, i) => sse("patch", { options: { [OSI]: { bid: 9 + i * 0.1, ask: 9.4 + i * 0.1 } } })).join("");
  const before = times.length;
  live.serve(sse("status", STATUS.live) + frames);
  await expect(priceInput(page, 1)).toHaveAttribute("placeholder", "11.10");      // the last tick lands
  await page.waitForTimeout(600);
  const burst = times.slice(before);
  for (let i = 1; i < burst.length; i++) expect(burst[i] - burst[i - 1]).toBeGreaterThanOrEqual(450);
  expect(burst.length).toBeLessThanOrEqual(4);
});

test("earnings inside the position: the IV-crush note and toggle, the IVs used, per-expiry vega, EV(HV30) withheld", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  const sent = calcRequests(page);
  await page.goto("/s/AAPL/calculator");
  await page.getByLabel("Base expiry").selectOption("2027-01-15");
  await page.getByLabel("Strategy template").selectOption("call-calendar");      // short Jan-15 / long Feb-19: earnings react Jan 29
  await expect(page.locator(".legs-table tbody tr")).toHaveCount(2);
  const note = page.getByTestId("earnings-note");
  await expect(note).toContainText("Earnings 2027-01-28 (implied move ±5.1%): IV crush modelled — back-month IV after earnings");
  await expect(page.locator(".legs-table tbody tr").nth(1).locator(".leg-iv-used")).toContainText("at 2027-01-15:");
  await expect(page.locator(".legs-table tbody tr").nth(0).locator(".leg-iv-used")).toHaveCount(0);   // the front leg keeps its IV
  await expect(stat(page, "Vega by expiry")).toContainText("2027-01-15");
  await expect(stat(page, "Vega by expiry")).toContainText("2027-02-19");
  await expect(stat(page, "EV (HV30)")).toHaveText(/\$/);                          // the front expiry is before the report
  // the toggle: legs keep their IV through the report
  await page.getByLabel("Model earnings IV crush").uncheck();
  await expect(note).toContainText("IV crush off");
  await expect.poll(() => sent.at(-1)?.earnings_crush).toBe(false);
  await expect(page.locator(".leg-iv-used")).toHaveCount(0);
  // only the Feb leg: the report falls before its expiry, so HV30 (no earnings move in it) gives no EV
  await page.getByRole("button", { name: "Remove leg" }).nth(0).click();
  await expect(stat(page, "EV (HV30)")).toContainText("Earnings react 2027-01-29, before 2027-02-19: HV30 holds no earnings move");
  expect(live.errors).toEqual([]);
});

test("streamed values are labelled delayed when the feed is delayed", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.delayed));
  await twoLegs(page);
  await page.locator(".legs-table tbody tr").nth(1).getByRole("button", { name: "Reset to live" }).click();
  live.serve(sse("status", STATUS.delayed) + sse("patch", {
    quotes: { AAPL: { price: 232.1, delayed: true, source: "questrade", ts: "2026-11-20T15:00:00Z" } },
    options: { [OSI]: { bid: 9.8, ask: 10.2, iv: 0.31, delayed: true, source: "questrade", ts: "2026-11-20T15:00:00Z" } },
  }));
  await expect(priceOrigin(page, 1)).toHaveAttribute("data-origin", "live");
  await expect(priceOrigin(page, 1)).toHaveText("delayed");
  await expect(page.getByTestId("spot-origin")).toHaveText("delayed");
  await expect(page.locator(".card-sub .asof")).toContainText("delayed as of");
  expect(live.errors).toEqual([]);
});
