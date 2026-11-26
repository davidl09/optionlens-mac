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
    await route.fulfill(res.text !== undefined ? { status: res.status, body: res.text, contentType: res.contentType } : { status: res.status, json: res.body });
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
  ["/reports/options-broad-view", "P/C history"],
  ["/reports?category=Strategies&symbols=SPX,NDX,RUT,XSP", "Symbol scope:"],
  ["/s/AAPL/contract/AAPL261120C00230000", "Daily history"],
  ["/backtest/stock", "Stock rule backtest"],
  ["/backtest/options", "Options strategy backtest"],
  ["/backtest/straddle-wing", "Straddle / wing (iron fly) backtest"],
  ["/compare/volatility?symbols=SPY,QQQ,IWM", "IV30 overlay"],
  ["/compare/straddle?symbols=SPY,QQQ&dte=30", "Compare straddles"],
  ["/watchlists", "Earnings plays"],
  ["/portfolio", "Positions"],
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
  await page.goto("/s/MSFT/chain");
  await expect(page.locator(".chain-source .badge")).toHaveText("Questrade");                // real-time: no delayed badge
  await page.goto("/status");
  await expect(page.getByRole("heading", { name: "Data sources" })).toBeVisible();
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
  // the calculator's legs stay marked in the chain: the bought ask green, the sold bid red
  await expect(page.locator("button.quote-btn.ask").nth(12)).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("button.quote-btn.bid").nth(16)).toHaveClass(/selected/);
  await expect(page.locator("button.quote-btn.selected")).toHaveCount(2);
  // clicking a marked quote takes that contract out of the calculator; clicking again puts it back
  await page.locator("button.quote-btn.ask").nth(12).click();
  await expect(page.getByText(/Removed .* · 1 leg in calculator/)).toBeVisible();
  await expect(page.locator("button.quote-btn.selected")).toHaveCount(1);
  await page.locator("button.quote-btn.ask").nth(12).click();
  await expect(page.getByText("2 legs in calculator")).toBeVisible();
  await page.getByRole("link", { name: "Open calculator →" }).click();
  await expect(page.locator(".legs-table tbody tr")).toHaveCount(2);
  await page.locator(".legs-table tbody tr").last().getByRole("button", { name: "Remove leg" }).click();   // the re-added ask
  await page.getByRole("link", { name: "Chain", exact: true }).click();      // a removed leg loses its mark
  await expect(page.locator("button.quote-btn.selected")).toHaveCount(1);
  await expect(page.locator("button.quote-btn.bid").nth(16)).toHaveClass(/selected/);
  await page.getByRole("link", { name: "Calculator", exact: true }).click();
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

test("Canadian earnings carry their source (TMX Money); Status lists the Canadian earnings sources", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/s/RY.TO/earnings");
  await expect(page.locator(".card", { hasText: "Next earnings" }).locator(".source-badge")).toHaveText("TMX Money");
  const history = page.locator(".card", { hasText: "Earnings history" });
  await expect(history.locator("th").filter({ hasText: "Source" })).toBeVisible();
  await expect(history.locator("td .badge", { hasText: "TMX Money" }).first()).toBeVisible();
  await page.goto("/s/AAPL/earnings");
  await expect(page.locator(".card", { hasText: "Next earnings" }).locator(".source-badge")).toHaveText("Nasdaq");
  await page.goto("/status");
  await expect(page.getByText("Earnings backfill (Canada)")).toBeVisible();
  await expect(page.getByText(/Yahoo Finance: Canadian listings' earnings dates \(fallback; unofficial endpoint\) · 429/)).toBeVisible();
  clean(p);
});

test("search, theme, bell", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/");
  await page.getByLabel("Search symbols").fill("sp");   // SPLG comes without a name: the list must still render
  await expect(page.locator(".search-list li", { hasText: "SPLG" })).toBeVisible();
  await page.getByLabel("Search symbols").fill("ms");
  await expect(page.locator(".search-list li").first()).toContainText("MSFT");
  await page.getByLabel("Search symbols").press("ArrowDown");
  await expect(page.locator(".search-list li").first()).toHaveAttribute("aria-selected", "true");   // visible keyboard highlight
  await expect(page.locator(".search-list li.active")).toHaveCount(1);
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

test("screenshots: dashboard, summary (dark + light), smallest window", async ({ page }) => {
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
  await page.setViewportSize({ width: 900, height: 600 });   // the desktop window's minimum size (electron/main/window.ts)
  for (const path of ["/", "/s/AAPL/chain", "/reports/unusual-option-volume"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `horizontal page overflow on ${path}`).toBeLessThanOrEqual(0);
  }
  await page.goto("/");
  await page.waitForLoadState("networkidle");
  await page.screenshot({ path: `${SHOTS}/dashboard-min-window.png`, fullPage: true });
  await page.getByRole("button", { name: "Menu" }).click();
  await page.getByRole("button", { name: "Strategies" }).click();
  await page.screenshot({ path: `${SHOTS}/menu-min-window.png` });
  clean(p);
});

test("a symbol without a stored chain (no bridge): options tabs show a neutral notice, not an error", async ({ page }) => {
  const p = await setup(page, [/404/]);
  for (const tab of ["chain", "calculator", "skew", "ideas"]) {
    await page.goto(`/s/DELL/${tab}`);
    await expect(page.locator(".chain-notice")).toContainText("No option chain stored for DELL yet.");
    await expect(page.locator(".error-box")).toHaveCount(0);
  }
  await page.goto("/s/XNOP/straddle");
  await expect(page.locator(".chain-notice")).toHaveText("No listed options for XNOP.");
  await expect(page.locator(".qh-name .badge.warn")).toHaveText("no options");
  clean(p);
});

test("empty report: the engine's reason, when chains are collected, and the method on request", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/reports/oi-changes");
  const empty = page.locator(".dt-empty");
  await expect(empty).toContainText("No stored option chain matches these settings.");
  await expect(empty).toContainText("Option chains are collected from your connected sources (IBKR and/or Questrade) during market hours, after hours for a last session that has none yet, and on demand when you open a symbol.");
  await expect(empty).toContainText("Last bridge push 2026-11-19 15:45 ET · next collection 2026-11-19 16:00 ET.");
  await expect(page.getByText("No data.", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Contracts not present in the prior end-of-day snapshot are skipped.")).toBeHidden();
  await page.getByText("Method", { exact: true }).click();
  await expect(page.getByText("Contracts not present in the prior end-of-day snapshot are skipped.")).toBeVisible();
  clean(p);
});

test("columns without any value are hidden by default, with a toggle", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/reports/market-movers");
  await expect(page.locator("th", { hasText: "Volume" })).toBeVisible();          // columns with data always show
  await expect(page.locator("th", { hasText: "IV30" })).toHaveCount(0);
  const toggle = page.getByLabel("Show empty columns (1)");
  await expect(toggle.locator("..")).toHaveAttribute("title", "No row has a value in: IV30");
  await toggle.check();
  await expect(page.locator("th", { hasText: "IV30" })).toBeVisible();
  clean(p);
});

test("dashboard: an empty unusual-volume panel is one line with the reason and the next collection", async ({ page }) => {
  const p = await setup(page);
  await page.route("**/api/market/overview", async (route) => {
    const m = handle("GET", new URL(route.request().url()), null)!.body as Record<string, unknown>;
    await route.fulfill({ json: { ...m, unusual_options: [], empty_notes: { unusual_options: "No option volume collected yet: the IBKR bridge collects it for every symbol during market hours." } } });
  });
  await page.goto("/");
  const notice = page.locator(".panel-notice");
  await expect(notice).toContainText("Unusual option volume: No option volume collected yet");
  await expect(notice).toContainText("next collection 2026-11-19 16:00 ET");
  await expect(page.locator(".card-head h3", { hasText: "Unusual option volume" })).toHaveCount(0);
  await expect(page.locator(".heat-tile", { hasText: "XLY" })).toContainText("Cons. Disc.");   // deliberate abbreviation
  clean(p);
});

test("after-hours fill: a small neutral badge on the quote, the chain, a report and the dashboard", async ({ page }) => {
  const p = await setup(page);
  const flagged = (path: string, patch: (b: Record<string, unknown>) => Record<string, unknown>) => page.route(path, async (route) => {
    const b = handle("GET", new URL(route.request().url()), null)!.body as Record<string, unknown>;
    await route.fulfill({ json: patch(b) });
  });
  await flagged("**/api/market/overview", (m) => ({ ...m, after_hours: true }));
  await flagged("**/api/reports/iv-rankings**", (r) => ({ ...r, after_hours: true }));
  await flagged("**/api/symbol/AAPL/summary", (s) => ({ ...s, quote: { ...(s.quote as object), after_hours: true } }));
  await flagged("**/api/symbol/AAPL/chain**", (c) => ({ ...c, after_hours: true }));
  const badge = page.locator(".badge.after-hours");
  await page.goto("/");
  await expect(badge).toHaveText("Last session's close (collected after hours)");
  await expect(badge).toHaveClass(/neutral/);
  await expect(badge).toHaveAttribute("title", /frozen data after the session ended/);
  await page.goto("/reports/iv-rankings");
  await expect(page.locator(".page-head .badge.after-hours")).toBeVisible();
  await page.goto("/s/AAPL/chain");
  await expect(page.locator(".qh-price .badge.after-hours")).toBeVisible();      // the quote
  await expect(page.locator(".toolbar .badge.after-hours")).toBeVisible();      // the chain
  await page.goto("/reports/unusual-option-volume");                            // not flagged: no badge
  await expect(page.getByText("Data notes:")).toBeVisible();
  await expect(page.locator(".page-head .badge.after-hours")).toHaveCount(0);
  clean(p);
});

test("report breadcrumb names the menu the user came from", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Market", exact: true }).click();
  await page.locator(".menu-panel").getByRole("link", { name: "Market movers" }).click();
  await expect(page.locator(".breadcrumb")).toHaveText("Market");
  await page.getByRole("button", { name: "Run" }).click();
  await expect(page.locator(".breadcrumb")).toHaveText("Market");
  await page.getByRole("button", { name: "Stocks", exact: true }).click();
  await page.locator(".menu-panel").getByRole("link", { name: "Market movers" }).click();
  await expect(page.locator(".breadcrumb")).toHaveText("Stocks");
  clean(p);
});

test("alert delete asks for confirmation", async ({ page }) => {
  const p = await setup(page);
  const deletes: string[] = [];
  page.on("request", (r) => { if (r.method() === "DELETE") deletes.push(new URL(r.url()).pathname); });
  await page.goto("/alerts");
  const row = page.locator("tr", { hasText: "AAPL: Price above $240" });
  page.once("dialog", (d) => { expect(d.message()).toContain("AAPL: Price above $240"); void d.dismiss(); });
  await row.getByRole("button", { name: "Delete" }).click();
  await page.waitForTimeout(200);
  expect(deletes).toEqual([]);
  page.once("dialog", (d) => void d.accept());
  await row.getByRole("button", { name: "Delete" }).click();
  await expect.poll(() => deletes).toEqual(["/api/alerts/1"]);
  clean(p);
});

test("options backtest: wing width only for strategies with wings; one-row period", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/backtest/options");
  await expect(page.getByLabel("Strategy")).toHaveValue("long-call");
  await expect(page.getByLabel("Wing width (%)")).toHaveCount(0);
  await page.getByLabel("Strategy").selectOption("bull-call-spread");
  await expect(page.getByLabel("Wing width (%)")).toBeVisible();
  const [start, end] = [await page.getByLabel("Start date").boundingBox(), await page.getByLabel("End date").boundingBox()];
  expect(Math.abs((start?.y ?? 0) - (end?.y ?? 1))).toBeLessThan(2);
  await page.goto("/backtest/straddle-wing");
  await expect(page.getByLabel("Wing width (%)")).toBeVisible();
  clean(p);
});

test("stock backtest: the entry rule select works with the keyboard", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/backtest/stock");
  const sel = page.getByLabel("Entry rule");
  await sel.focus();
  await page.keyboard.press("ArrowDown");
  await expect(sel).toHaveValue("day_of_month");
  await expect(page.getByText("Day", { exact: true })).toBeVisible();
  clean(p);
});

test("chart: SMAs from the full stored history; a too-short history is said, not drawn", async ({ page }) => {
  const p = await setup(page, [/404/]);
  await page.goto("/s/AAPL/chart?range=1m&interval=1d");   // 1M of daily bars (1M is 15-min bars by default)
  await expect(page.locator(".legend-note")).toContainText("SMAs use the full stored price history");
  await expect(page.locator(".legend-note")).not.toContainText("not enough stored history");
  await page.goto("/s/DELL/chart");
  await expect(page.locator(".legend-note")).toContainText("SMA 200: not enough stored history yet");
  await expect(page.locator(".legend-note")).not.toContainText("SMA 50:");
  clean(p);
});

test("data status: zero-archive EOD is a warning, details readable, chain coverage explained", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/status");
  const row = page.locator("tr", { hasText: "nothing archived" });
  await expect(row.locator(".badge")).toHaveText("warning");
  await expect(row.locator(".badge")).toHaveClass(/warn/);
  await expect(page.getByText("With a recent chain")).toBeVisible();
  await expect(page.getByText("14 confirmed none")).toBeVisible();
  clean(p);
});

test("symbol page: fundamentals as numbers, insider sells split, IV30 legend only with data", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/s/AAPL/insiders");
  await expect(page.getByText(/Of the \d+ sells listed below: \d+ automatic \(10b5-1 plan\), \d+ open market/)).toBeVisible();
  await page.goto("/s/AAPL/dividends");
  await expect(page.getByText("Ex-dates measured")).toBeVisible();
  const history = page.locator("section.card", { has: page.getByRole("heading", { name: "History", exact: true }) });
  await expect(history.locator("th", { hasText: /^Source(?!s)/ })).toBeVisible();
  await expect(history.locator(".source-badge").first()).toHaveText("Nasdaq");     // provenance on every row
  await expect(page.getByText(/Nasdaq-listed securities only/)).toHaveCount(0);
  await page.goto("/s/AAPL/fundamentals");
  const key = page.locator("section.card", { has: page.getByRole("heading", { name: "Key figures" }) });
  await expect(key).toContainText("P/E");
  await expect(key.locator(".source-badge").first()).toHaveText("Questrade");
  clean(p);
});

test("compare straddles: a symbol without a stored chain is named, with when chains are collected", async ({ page }) => {
  const p = await setup(page);
  await page.goto("/compare/straddle?symbols=SPY,DELL&dte=30");
  await expect(page.locator(".info-box", { hasText: "No stored option chain for DELL yet" })).toContainText("Option chains are collected from your connected sources");
  await expect(page.locator("td", { hasText: "DELL" })).toBeVisible();
  clean(p);
});
