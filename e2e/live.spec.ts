// Live end-to-end suite: drives the built SPA served by a REAL OptionLens server (no mocks).
// Skipped unless LIVE_BASE_URL is set, e.g.
//   LIVE_BASE_URL=http://127.0.0.1:8480 npx playwright test e2e/live.spec.ts --trace off
// Every page fails on console errors, page errors, /api responses with status >= 400, an error box,
// or visible "Error" / "undefined" / "NaN" / "[object Object]" text.
import { APIRequestContext, expect, Page, test } from "@playwright/test";
import { mkdirSync } from "node:fs";

const BASE = process.env.LIVE_BASE_URL;
const SHOTS = "e2e/screenshots/live";
const SYMBOLS = ["AAPL", "KO", "SPY", "SPX", "TSLA"];
const TABS = ["summary", "chart", "price-action", "dividends", "chain", "activity", "open-interest", "calculator", "ideas",
  "straddle", "volatility", "skew", "implied-moves", "vol-seasonality", "earnings", "seasonality", "events", "fundamentals",
  "insiders", "news", "about"];
const SHOT_PAGES = new Set(["/", "/s/AAPL/summary", "/s/AAPL/chain", "/s/AAPL/earnings", "/s/AAPL/volatility", "/s/SPX/summary",
  "/s/SPX/straddle", "/s/KO/dividends", "/s/TSLA/ideas", "/s/SPY/activity", "/s/AAPL/open-interest", "/s/AAPL/fundamentals",
  "/reports/unusual-option-volume", "/reports/stock-screener", "/reports/earnings-option-strategy-screener",
  "/reports/covered-calls", "/reports/iv-rankings", "/status"]);
const BAD_TEXT = [/\bError\b/, /\bundefined\b/, /\bNaN\b/, /\[object Object\]/];
const REPORT_SHARDS = 4;

test.skip(!BASE, "set LIVE_BASE_URL to run the live suite");
test.use({ baseURL: BASE });
test.setTimeout(600_000);
mkdirSync(SHOTS, { recursive: true });

interface Problems { list: string[] }

function watch(page: Page): Problems {
  const p: Problems = { list: [] };
  page.on("pageerror", (e) => p.list.push(`pageerror: ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") p.list.push(`console: ${m.text()}`); });
  page.on("response", async (r) => {
    const u = new URL(r.url());
    if (!u.pathname.startsWith("/api/") || r.status() < 400) return;
    const body = await r.text().catch(() => "");
    p.list.push(`HTTP ${r.status()} ${r.request().method()} ${u.pathname}${u.search} ${r.request().postData() ?? ""} -> ${body.slice(0, 300)}`);
  });
  page.on("requestfailed", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/api/") && !/ERR_ABORTED/.test(r.failure()?.errorText ?? "")) p.list.push(`request failed ${u.pathname}: ${r.failure()?.errorText}`);
  });
  return p;
}

/** Waits for the network and every spinner to settle. */
async function settle(page: Page) {
  await page.waitForLoadState("networkidle", { timeout: 120_000 });
  await expect(page.locator(".spinner")).toHaveCount(0, { timeout: 120_000 });
  await page.waitForLoadState("networkidle", { timeout: 120_000 });
}

/** Problems visible on the current page (error boxes, bad text). */
async function pageProblems(page: Page, where: string): Promise<string[]> {
  const out: string[] = [];
  const boxes = await page.locator(".error-box").allInnerTexts();
  for (const b of boxes) out.push(`${where}: error box "${b.slice(0, 200)}"`);
  const text = await page.locator("body").innerText();
  for (const re of BAD_TEXT) {
    const m = text.match(new RegExp(`.{0,60}${re.source}.{0,60}`));
    if (m) out.push(`${where}: visible ${re} in "${m[0].replace(/\s+/g, " ")}"`);
  }
  return out;
}

function shotName(path: string): string {
  return path.replace(/^\//, "").replace(/[^a-zA-Z0-9]+/g, "_") || "dashboard";
}

/** Visits path, returns all problems found (does not throw, so one test can cover many pages). */
async function visit(page: Page, p: Problems, path: string): Promise<string[]> {
  p.list.length = 0;
  try {
    await page.goto(path);
    await settle(page);
  } catch (e) {
    return [`${path}: ${(e as Error).message.split("\n")[0]}`];
  }
  const out = [...p.list.map((x) => `${path}: ${x}`), ...(await pageProblems(page, path))];
  if (SHOT_PAGES.has(path)) await page.screenshot({ path: `${SHOTS}/${shotName(path)}.png`, fullPage: true });
  return out;
}

async function visitAll(page: Page, paths: string[]) {
  const p = watch(page);
  const problems: string[] = [];
  for (const path of paths) {
    await test.step(path, async () => { problems.push(...(await visit(page, p, path))); });
  }
  expect(problems, "problems").toEqual([]);
}

async function reportIds(request: APIRequestContext): Promise<string[]> {
  const r = await request.get("/api/reports");
  expect(r.ok()).toBeTruthy();
  return ((await r.json()) as { id: string }[]).map((d) => d.id);
}

test.describe.configure({ mode: "parallel" });

test("market pages", async ({ page }) => {
  await visitAll(page, ["/", "/reports", "/reports?category=Strategies&symbols=SPX,NDX,RUT,XSP", "/status", "/about",
    "/compare/volatility?symbols=SPY,QQQ,IWM", "/compare/straddle?symbols=SPY,QQQ,AAPL&dte=30"]);
});

for (let k = 0; k < REPORT_SHARDS; k++) {
  test(`reports shard ${k + 1}/${REPORT_SHARDS}`, async ({ page, request }) => {
    const ids = (await reportIds(request)).filter((_, i) => i % REPORT_SHARDS === k);
    expect(ids.length).toBeGreaterThan(0);
    await visitAll(page, ids.map((id) => `/reports/${id}`));
  });
}

for (const sym of SYMBOLS) {
  test(`symbol ${sym} tabs`, async ({ page }) => {
    await visitAll(page, TABS.map((t) => `/s/${sym}/${t}`));
  });
}

test("contract page from the AAPL chain", async ({ page, request }) => {
  const r = await request.get("/api/symbol/AAPL/chain");
  expect(r.ok()).toBeTruthy();
  const chain = (await r.json()) as { calls: { osi: string; volume: number }[] };
  expect(chain.calls.length).toBeGreaterThan(0);
  const osi = [...chain.calls].sort((a, b) => b.volume - a.volume)[0].osi;
  await visitAll(page, [`/s/AAPL/contract/${osi}`]);
  await expect(page.getByText("Daily history").first()).toBeVisible();
});

test("calculator template", async ({ page }) => {
  const p = watch(page);
  await page.goto("/s/AAPL/calculator");
  await settle(page);
  await page.getByLabel("Strategy template").selectOption("iron-butterfly-short");
  await expect(page.locator(".legs-table tbody tr")).toHaveCount(4);
  await settle(page);
  await expect(page.getByText("Breakevens").first()).toBeVisible();
  await expect(page.getByText("Prob. of profit").first()).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/calculator.png`, fullPage: true });
  expect([...p.list, ...(await pageProblems(page, "calculator"))]).toEqual([]);
});

test("backtests", async ({ page }) => {
  const p = watch(page);
  const problems: string[] = [];
  for (const path of ["/backtest/stock", "/backtest/options", "/backtest/straddle-wing"]) {
    await page.goto(path);
    await settle(page);
    await page.getByRole("button", { name: "Run backtest" }).click();
    await settle(page);
    await expect(page.getByText("Equity curve").first()).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/${shotName(path)}.png`, fullPage: true });
    problems.push(...p.list.map((x) => `${path}: ${x}`), ...(await pageProblems(page, path)));
    p.list.length = 0;
  }
  expect(problems).toEqual([]);
});

test("watchlist lifecycle", async ({ page }) => {
  const p = watch(page);
  page.on("dialog", (d) => void d.accept());
  const name = `live-e2e-${Date.now()}`;
  await page.goto("/watchlists");
  await settle(page);
  await page.getByLabel("New watchlist name").fill(name);
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.locator(".card-titles h3", { hasText: name })).toBeVisible();
  await page.getByLabel("Add symbols").fill("TSLA, AAPL, KO");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("3 symbols")).toBeVisible();
  await expect(page.locator(".chips .chip").first()).toContainText("TSLA");   // insertion order kept
  await settle(page);
  await expect(page.locator(".dt tbody tr")).toHaveCount(3);
  await page.screenshot({ path: `${SHOTS}/watchlist.png`, fullPage: true });
  const problems = [...p.list, ...(await pageProblems(page, "watchlists"))];
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(page.locator(".card-titles h3", { hasText: name })).toHaveCount(0);
  expect([...problems, ...p.list]).toEqual([]);
});

test("alert lifecycle", async ({ page }) => {
  const p = watch(page);
  await page.goto("/alerts");
  await settle(page);
  await page.locator("#al-kind").selectOption("price_above");
  await page.locator("#al-value").fill("98765");
  await page.locator("#al-sym").fill("AAPL");
  await page.getByRole("button", { name: "Create alert" }).click();
  const row = page.locator("tr", { hasText: "AAPL: Price above $98765" });
  await expect(row).toBeVisible();
  await settle(page);
  const problems = [...p.list, ...(await pageProblems(page, "alerts"))];
  await row.getByRole("button", { name: "Delete" }).click();
  await expect(row).toHaveCount(0);
  expect([...problems, ...p.list]).toEqual([]);
});
