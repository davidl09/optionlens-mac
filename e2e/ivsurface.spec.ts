// The Skew page's IV surface against the mocked engine: the heatmap renders, Rows / Colour switch the request and the
// scale, the earnings expiry is marked, and a click on a reliable cell adds its contract to the calculator (no price:
// it follows the live mid) and outlines the cell. Cells are found from the heatmap's fixed plot area (HEAT_GRID).
import { expect, type Locator, type Page, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { HEAT_GRID } from "../src/components/charts/common";
import { sse, STATUS } from "./mocks/live";
import { setup } from "./mocks/stream";

const SHOTS = "e2e/screenshots";
mkdirSync(SHOTS, { recursive: true });
const EXPIRIES = 8;                       // the mock's columns
const DELTA_ROWS = 17;                    // 10..45Δ puts, ATM, 45..10Δ calls (y index 0 at the bottom)

const panel = (page: Page) => page.locator("section.card", { has: page.locator("h3", { hasText: /^IV surface$/ }) });
const chart = (page: Page) => panel(page).locator('.echart[aria-label="IV surface heatmap"]');
const tooltip = (page: Page) => chart(page).locator("div").filter({ hasText: "DTE" }).last();

/** Page coordinates of the centre of cell (x, y) and of a point just inside its left edge. */
async function cellAt(c: Locator, x: number, y: number, rows: number) {
  await c.scrollIntoViewIfNeeded();
  const b = (await c.boundingBox())!;
  const w = (b.width - HEAT_GRID.left - HEAT_GRID.right) / EXPIRIES;
  const h = (b.height - HEAT_GRID.top - HEAT_GRID.bottom) / rows;
  const cx = b.x + HEAT_GRID.left + (x + 0.5) * w;
  const cy = b.y + b.height - HEAT_GRID.bottom - (y + 0.5) * h;
  return { cx, cy, edge: { x: Math.round(cx - w / 2 + 1.5 - b.x), y: Math.round(cy - b.y) } };
}

/** The canvas pixel (r, g, b) at a point of the chart. */
async function pixel(c: Locator, at: { x: number; y: number }): Promise<number[]> {
  return c.locator("canvas").first().evaluate((cv: HTMLCanvasElement, p) => Array.from(cv.getContext("2d")!.getImageData(p.x, p.y, 1, 1).data).slice(0, 3), at);
}

function surfaceRequests(page: Page): URL[] {
  const out: URL[] = [];
  page.on("request", (r) => { const u = new URL(r.url()); if (u.pathname.endsWith("/iv-surface")) out.push(u); });
  return out;
}

test("IV surface: renders, switches rows and colour, marks the earnings expiry", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  const sent = surfaceRequests(page);
  await page.goto("/s/AAPL/skew");
  await expect(chart(page)).toBeVisible();
  expect(sent.at(-1)!.searchParams.get("rows")).toBe("delta");
  expect(sent.at(-1)!.searchParams.get("mode")).toBe("iv");
  const legend = panel(page).getByTestId("heat-legend");
  await expect(legend).toContainText("unreliable");
  await expect(legend).not.toContainText("+");                           // IV: a sequential scale, no signs

  const marker = panel(page).getByTestId("earnings-marker");
  await expect(marker).toContainText("first expiry after earnings Jan 28");
  await expect(marker).toHaveAttribute("title", /Earnings 2027-01-28 .*2027-02-19 is the first expiry after it/);
  const earn = await cellAt(chart(page), 5, 4, DELTA_ROWS);              // 2027-02-19 column, 30Δ put
  await page.mouse.move(earn.cx, earn.cy);
  await expect(tooltip(page)).toContainText("First expiry after earnings (2027-01-28)");
  await expect(tooltip(page)).toContainText("30Δ put");

  const front = await cellAt(chart(page), 0, 0, DELTA_ROWS);             // front expiry 10Δ put: unreliable in the mock
  await page.mouse.move(front.cx, front.cy);
  await expect(tooltip(page)).toContainText("Unreliable: no bid");

  await panel(page).getByRole("radio", { name: "vs ATM" }).click();
  await expect.poll(() => sent.at(-1)?.searchParams.get("mode")).toBe("vs_atm");
  await expect(legend).toContainText(/^-(\d+\.\d+)%\+\1%/);           // diverging: symmetric around zero
  await panel(page).getByRole("radio", { name: "Change 1d" }).click();
  await expect(panel(page).locator(".card-sub")).toContainText("change vs 2026-11-18 close");
  await Promise.all([page.waitForResponse((r) => r.url().includes("rows=moneyness")), panel(page).getByRole("radio", { name: "Moneyness" }).click()]);
  await page.waitForTimeout(100);                                         // the new option is drawn
  const m = await cellAt(chart(page), 3, 2, 17);
  await page.mouse.move(m.cx, m.cy);
  await expect(tooltip(page)).toContainText("−15%");
  await panel(page).getByRole("radio", { name: "Strike" }).click();
  await expect.poll(() => sent.at(-1)?.searchParams.get("rows")).toBe("strike");
  await expect(chart(page)).toBeVisible();
  expect(live.errors).toEqual([]);
});

test("IV surface: a click adds the contract to the calculator and outlines its cell", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  await page.goto("/s/AAPL/skew");
  await expect(chart(page)).toBeVisible();
  const c = await cellAt(chart(page), 3, 3, DELTA_ROWS);                 // 2026-12-18, 25Δ put
  await page.mouse.move(c.cx, c.cy);
  await expect(tooltip(page)).toContainText("(29 DTE) · 25Δ put");
  await expect(tooltip(page)).toContainText("Click to add to the calculator");
  const strike = (await tooltip(page).textContent())!.match(/Contract (\d+(?:\.\d+)?) put/)![1];
  const before = await pixel(chart(page), c.edge);
  await page.mouse.click(c.cx, c.cy);
  const toast = page.getByRole("status").filter({ hasText: "Added" });
  await expect(toast).toContainText(`Added +1 P ${strike} 2026-12-18 at the live mid · 1 leg in calculator`);
  await expect.poll(async () => (await pixel(chart(page), c.edge)).join(",")).not.toBe(before.join(","));
  const after = await pixel(chart(page), c.edge);
  expect(after.reduce((a, b) => a + b, 0)).toBeGreaterThan(before.reduce((a, b) => a + b, 0));   // the light outline (dark theme)
  await page.mouse.move(c.cx, c.cy - 200);
  await page.mouse.move(c.cx, c.cy);
  await expect(tooltip(page)).toContainText("In the calculator — click to remove it");
  await page.mouse.click(c.cx, c.cy);                                    // an outlined cell: the contract comes out
  await expect(page.getByRole("status").filter({ hasText: "Removed" })).toContainText(`Removed +1 P ${strike} 2026-12-18 · 0 legs in calculator`);
  await expect.poll(async () => (await pixel(chart(page), c.edge)).join(",")).toBe(before.join(","));
  await page.mouse.click(c.cx, c.cy);                                    // and back in
  await expect(toast).toContainText("1 leg in calculator");
  await page.mouse.move(c.cx, c.cy - 200);

  const unreliable = await cellAt(chart(page), 0, 0, DELTA_ROWS);
  await page.mouse.click(unreliable.cx, unreliable.cy);                 // greyed: not added
  await expect(toast).toContainText("1 leg in calculator");
  await page.mouse.move(0, 0);
  await panel(page).screenshot({ path: `${SHOTS}/iv-surface-dark.png` });

  await toast.getByRole("link", { name: "Open calculator →" }).click();
  await expect(page.locator(".legs-table tbody tr")).toHaveCount(1);
  await expect(page.locator(".legs-table tbody tr").first()).toContainText(strike);

  await page.goto("/s/AAPL/skew");
  await page.getByRole("button", { name: /Switch to light theme/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(chart(page)).toBeVisible();
  const [r, g, b] = await pixel(chart(page), (await cellAt(chart(page), 0, 0, DELTA_ROWS)).edge);
  expect(r + g + b).toBeGreaterThan(600);                                // the light theme's greyed cell, re-read on toggle
  await page.mouse.move(0, 0);
  await panel(page).screenshot({ path: `${SHOTS}/iv-surface-light.png` });
  expect(live.errors).toEqual([]);
});
