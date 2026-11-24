// My → Portfolio against a mocked engine (INTEGRATION.md §11): positions from both brokers grouped by underlying with
// live repricing and flashes, totals with the broker's FX rate, the risk graph, "Open in calculator" (cost basis as
// pinned prices), on-demand refresh, the reporting currency, and the states: nothing held, Questrade missing its
// account permission, no broker connected.
import { expect, test } from "@playwright/test";
import { sse, STATUS } from "./mocks/live";
import { NEEDS_PERMISSION, refreshes, resetPortfolio, setScenario } from "./mocks/portfolio";
import { setup } from "./mocks/stream";

const OSI = "AAPL261218C00250000";

test.beforeEach(() => resetPortfolio());

test("positions grouped by underlying, totals with the broker's FX rate, sources and masked accounts", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  await page.goto("/");
  await page.getByRole("button", { name: "My" }).click();
  await page.getByRole("link", { name: "Portfolio" }).click();
  await expect(page.getByRole("heading", { name: "Portfolio", level: 1 })).toBeVisible();
  await expect(page.getByText("Read-only: OptionLens shows your positions")).toBeVisible();
  await expect(page.getByTestId("source-questrade")).toHaveAttribute("data-state", "ok");
  await expect(page.getByTestId("source-ibkr")).toContainText("IBKR: connected");
  await expect(page.getByTestId("fx-label")).toHaveText(/1 USD = 1\.3800 CAD \(Questrade balances/);
  await expect(page.getByTestId("group-AAPL")).toContainText("2 positions");
  await expect(page.getByTestId("group-AAPL").getByRole("link", { name: "AAPL" })).toHaveAttribute("href", "/s/AAPL");
  await expect(page.getByTestId("group-THI.TO").getByRole("link")).toHaveCount(0);          // not a US listing
  await expect(page.getByTestId(`leg-${OSI}`)).toContainText("Call 250 · Dec 18, 26");
  await expect(page.getByTestId(`leg-${OSI}`)).toContainText("Margin •••145");
  await expect(page.getByTestId("leg-AAPL")).toContainText("•••567");
  await expect(page.getByTestId("leg-THI.TO")).toContainText("CA$60.17");
  const totals = page.locator("section.card").filter({ has: page.getByRole("heading", { name: "Totals" }) });
  await expect(totals.locator(".stat", { hasText: "Beta-weighted Δ (SPY)" })).toContainText("39 sh");
  await expect(totals.locator(".stat", { hasText: "Net liquidation" })).toContainText("$");
  await page.getByRole("button", { name: "Collapse AAPL" }).click();
  await expect(page.getByTestId(`leg-${OSI}`)).toHaveCount(0);
  await page.getByRole("button", { name: "Expand AAPL" }).click();
  await expect(page.getByTestId(`leg-${OSI}`)).toBeVisible();
  // the held US symbols and contracts (and SPY) are kept live
  await expect.poll(() => live.subscriptions.at(-1)?.option_contracts ?? []).toContain(OSI);
  expect(live.subscriptions.at(-1)!.symbols).toEqual(expect.arrayContaining(["AAPL", "SPY"]));
  await page.screenshot({ path: "e2e/screenshots/portfolio.png", fullPage: true });
  expect(live.errors).toEqual([]);
});

test("live: repriced every few seconds while quotes flow, with flashes", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.live));
  await page.goto("/portfolio");
  const price = page.getByTestId("leg-AAPL").locator("td").nth(4);
  const first = await price.textContent();
  await expect(price).not.toHaveText(first!, { timeout: 8_000 });
  await expect.poll(async () => Number(await price.locator("span[data-flashes]").getAttribute("data-flashes") ?? 0)).toBeGreaterThan(0);
  expect(refreshes.every((r) => r === false)).toBe(true);                                  // polls never force a broker fetch
});

test("risk graph and open in calculator with the cost basis pinned", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  await page.goto("/portfolio");
  await page.getByTestId("group-AAPL").getByRole("button", { name: "Risk graph" }).click();
  const panel = page.locator("section.card").filter({ has: page.getByRole("heading", { name: "Risk graph · AAPL" }) });
  await expect(panel.locator("canvas, svg").first()).toBeVisible();
  await expect(panel.locator(".stat", { hasText: "Expiry shown" })).toContainText("2026");
  await page.getByTestId("group-AAPL").getByRole("button", { name: "Open in calculator" }).click();
  await expect(page).toHaveURL(/\/s\/AAPL\/calculator$/);
  await expect(page.locator(".legs-table tbody tr")).toHaveCount(2);
  const origins = page.locator(".legs-table tbody tr .leg-origin[data-origin='user']");
  await expect(origins).toHaveCount(2);                                                    // both legs: your price
  await expect(page.getByLabel("Entry price").nth(0)).toHaveValue("200");
  await expect(page.getByLabel("Entry price").nth(1)).toHaveValue("4.1");
});

test("refresh fetches the brokers now; the reporting currency switches", async ({ page }) => {
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  const urls: string[] = [];
  page.on("request", (r) => { if (new URL(r.url()).pathname === "/api/portfolio") urls.push(r.url()); });
  await page.goto("/portfolio");
  await expect(page.getByTestId("group-AAPL")).toBeVisible();
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => refreshes.filter(Boolean).length).toBe(1);
  await page.getByRole("radio", { name: "CAD" }).click();
  await expect.poll(() => urls.some((u) => u.includes("currency=CAD"))).toBe(true);
  expect(refreshes.filter(Boolean).length).toBe(1);
});

test("Questrade without the account permission says what to do", async ({ page }) => {
  setScenario("needs_permission");
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  await page.goto("/portfolio");
  const q = page.getByTestId("source-questrade");
  await expect(q).toHaveAttribute("data-state", "needs_permission");
  await expect(q.getByRole("alert")).toContainText(NEEDS_PERMISSION);
  await expect(q.getByRole("link", { name: "Settings → Data sources" })).toHaveAttribute("href", "/settings");
});

test("empty states: nothing held, and no broker connected", async ({ page }) => {
  setScenario("empty");
  const live = await setup(page);
  live.serve(sse("status", STATUS.closed));
  await page.goto("/portfolio");
  await expect(page.getByText("No open positions.")).toBeVisible();
  setScenario("none");
  live.serve(sse("status", STATUS.closed));
  await page.reload();
  await expect(page.getByText("Connect a broker to see your positions here")).toBeVisible();
  await expect(page.getByTestId("source-questrade")).toHaveAttribute("data-state", "not_connected");
});
