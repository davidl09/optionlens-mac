// Smoke test: serves the production build (vite preview), mocks /api/**, visits every route,
// and fails on console errors, page errors or unmocked endpoints.
import { expect, Page, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { handle } from "./mocks";

const SHOTS = "e2e/screenshots";
mkdirSync(SHOTS, { recursive: true });

interface Problems { errors: string[]; unmocked: string[] }

async function setup(page: Page, allowConsole: RegExp[] = []): Promise<Problems> {
  const p: Problems = { errors: [], unmocked: [] };
  page.on("pageerror", (e) => p.errors.push(`pageerror: ${e.message}`));
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    if (!allowConsole.some((r) => r.test(text))) p.errors.push(`console: ${text}`);
  });
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    let body: unknown = null;
    try { body = req.postDataJSON(); } catch { body = null; }
    const res = handle(req.method(), url, body);
    if (!res) { p.unmocked.push(`${req.method()} ${url.pathname}${url.search}`); await route.fulfill({ status: 501, json: { detail: "not mocked" } }); return; }
    await route.fulfill({ status: res.status, json: res.body });
  });
  return p;
}

function clean(p: Problems) {
  expect(p.unmocked, "unmocked API calls").toEqual([]);
  expect(p.errors, "console / page errors").toEqual([]);
}

const SYMBOL_TABS: [string, string | RegExp][] = [
  ["summary", "Key stats"], ["chart", "Price chart"], ["price-action", "Daily history"], ["dividends", "Dividend profile"],
  ["chain", "Calls"], ["activity", "Volume by expiry"], ["open-interest", "Max pain by expiry"], ["calculator", "Legs"],
  ["ideas", "Ranked by edge"], ["straddle", "ATM straddle by expiry"], ["volatility", "Constant-maturity IV"], ["skew", "IV smile"],
  ["implied-moves", "Implied move by expiry"], ["vol-seasonality", "Average volatility by calendar month"], ["earnings", "Earnings history"],
  ["seasonality", "Monthly returns by year"], ["events", "Upcoming"], ["fundamentals", "Financial statements"], ["insiders", "Transactions"],
  ["news", "News · AAPL"], ["about", "Similar stocks"],
];

const PAGES: [string, string | RegExp][] = [
  ["/", "VIX term structure"],
  ["/reports", "Reports & screeners"],
  ["/reports/unusual-option-volume", "Data notes:"],
  ["/reports/covered-calls", "Covered calls"],
  ["/reports/stock-screener", "Stock screener"],
  ["/reports/options-broad-view", "Pc history"],
  ["/reports?category=Strategies&symbols=SPX,NDX,RUT,XSP", "Symbol scope:"],
  ["/s/AAPL/contract/AAPL261120C00230000", "Daily history"],
  ["/backtest/stock", "Stock rule backtest"],
  ["/backtest/options", "Options strategy backtest"],
  ["/backtest/straddle-wing", "Straddle / wing backtest"],
  ["/compare/volatility?symbols=SPY,QQQ,IWM", "IV30 overlay"],
  ["/compare/straddle?symbols=SPY,QQQ&dte=30", "Compare straddles"],
  ["/watchlists", "Earnings plays"],
  ["/alerts", "Your alerts"],
  ["/status", "Job runs"],
  ["/about", "Not available (no free source)"],
  ["/settings", "available in the OptionLens desktop app"],
  ["/no/such/page", "Page not found"],
];

test.describe("every route renders cleanly", () => {
  for (const [path, text] of PAGES) {
    test(`page ${path}`, async ({ page }) => {
      const p = await setup(page);
      await page.goto(path);
      await expect(page.getByText(text).first()).toBeVisible();
      await page.waitForLoadState("networkidle");
      clean(p);
    });
  }
  for (const [tab, text] of SYMBOL_TABS) {
    test(`symbol tab ${tab}`, async ({ page }) => {
      const p = await setup(page);
      await page.goto(`/s/AAPL/${tab}`);
      await expect(page.locator(".quote-header h1")).toHaveText("AAPL");
      await expect(page.getByText(text).first()).toBeVisible();
      await page.waitForLoadState("networkidle");
      clean(p);
    });
  }
});

test("unknown symbol shows an error box", async ({ page }) => {
  const p = await setup(page, [/status of 404/]);
  await page.goto("/s/NOPE/summary");
  await expect(page.getByText("Unknown symbol NOPE").first()).toBeVisible();
  clean(p);
});

test("report: filters, sort and URL state", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/reports/stock-screener");
  await expect(page.getByText("600 rows")).toBeVisible();
  await expect(page.locator(".dt-pager")).toBeVisible();
  await page.getByRole("button", { name: "Filter IV30" }).click();
  await page.getByLabel("IV30 minimum").fill("50");
  await expect(page.getByText(/of 600 rows/)).toBeVisible();
  expect(new URL(page.url()).searchParams.get("_f")).toContain("iv30");
  await page.keyboard.press("Escape");
  await page.locator("th").filter({ hasText: "Price" }).locator(".th-sort").click();
  expect(new URL(page.url()).searchParams.get("_sort")).toBe("price:desc");
  await page.getByRole("button", { name: "Run" }).click();
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${SHOTS}/report.png` });
  clean(p);
});

test("source badges and chain-universe coverage note", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/reports/covered-calls");
  await expect(page.locator(".coverage-note")).toContainText("chain universe");
  await expect(page.locator("th").filter({ hasText: "Source" })).toBeVisible();
  await expect(page.locator("td .badge", { hasText: "reconstructed" }).first()).toBeVisible();
  await page.goto("/s/AAPL/chain");
  await expect(page.locator(".chain-source")).toContainText("IBKR");
  await expect(page.locator(".chain-source")).toContainText("delayed");
  await page.goto("/status");
  await expect(page.getByText("Data sources")).toBeVisible();
  await expect(page.getByText("optionlens-mac 0.1.0")).toBeVisible();
  clean(p);
});

test("chain → calculator flow", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/s/AAPL/chain");
  await expect(page.locator(".spot-row")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/chain.png` });
  await page.locator("button.quote-btn.ask").nth(12).click();
  await page.locator("button.quote-btn.bid").nth(16).click();
  await expect(page.getByText("2 legs in calculator")).toBeVisible();
  await page.getByRole("link", { name: "Open calculator →" }).click();
  await expect(page.locator(".legs-table tbody tr")).toHaveCount(2);
  await expect(page.getByText("Position metrics")).toBeVisible();
  await expect(page.getByText("Prob. of profit")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${SHOTS}/calculator.png`, fullPage: true });
  clean(p);
});

test("calculator templates", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/s/AAPL/calculator");
  await page.getByLabel("Strategy template").selectOption("iron-butterfly-short");
  await expect(page.locator(".legs-table tbody tr")).toHaveCount(4);
  await expect(page.getByText("Breakevens")).toBeVisible();
  clean(p);
});

test("backtests run", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/backtest/stock");
  await page.getByRole("button", { name: "Run backtest" }).click();
  await expect(page.getByText("Equity curve")).toBeVisible();
  await page.goto("/backtest/straddle-wing");
  await expect(page.locator("select").nth(0)).toHaveValue("iron-butterfly-short");
  await page.getByRole("button", { name: "Run backtest" }).click();
  await expect(page.getByText("Return distribution")).toBeVisible();
  clean(p);
});

test("earnings strategy drill-down", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/s/AAPL/earnings");
  await page.waitForLoadState("networkidle");
  await page.getByRole("cell", { name: "Long straddle", exact: true }).click();
  await expect(page.getByText(/Trades · Long straddle/)).toBeVisible();
  await expect(page.getByText("+1 C 230 / +1 P 230").first()).toBeVisible();
  clean(p);
});

test("search, theme, bell", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/");
  await page.getByLabel("Search symbols").fill("ms");
  await expect(page.locator(".search-list li").first()).toContainText("MSFT");
  await page.getByLabel("Search symbols").press("ArrowDown");
  await page.getByLabel("Search symbols").press("Enter");
  await expect(page).toHaveURL(/\/s\/MSFT\/summary/);
  await page.getByRole("button", { name: /Notifications \(2 unread\)/ }).click();
  await expect(page.getByText("Morning digest")).toBeVisible();
  await page.getByRole("button", { name: /Switch to light theme/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.getByRole("button", { name: "Options", exact: true }).click();
  await expect(page.locator(".menu-panel").getByText("IV rankings")).toBeVisible();
  clean(p);
});

test("screenshots: dashboard, summary (dark + light), phone", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/");
  await expect(page.getByText("Sector ETFs")).toBeVisible();
  await expect(page.getByText("No tracked company reports on the next session")).toBeVisible();   // explained empty panel
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${SHOTS}/dashboard.png`, fullPage: true });
  await page.goto("/s/AAPL/summary");
  await expect(page.getByText("Key stats")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${SHOTS}/summary.png`, fullPage: true });
  await page.goto("/s/AAPL/volatility");
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${SHOTS}/volatility.png`, fullPage: true });
  await page.getByRole("button", { name: /Switch to light theme/ }).click();
  await page.goto("/s/AAPL/summary");
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${SHOTS}/summary-light.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ["/", "/s/AAPL/chain", "/reports/unusual-option-volume"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `horizontal page overflow on ${path}`).toBeLessThanOrEqual(0);
  }
  await page.goto("/");
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${SHOTS}/dashboard-phone.png`, fullPage: true });
  await page.getByRole("button", { name: "Menu" }).click();
  await page.getByRole("button", { name: "Strategies" }).click();
  await page.screenshot({ path: `${SHOTS}/menu-phone.png` });
  clean(p);
});
