// Desktop-mode smoke: a fake preload API (window.optionlens) stands in for the Electron main process.
// Checks the engine gate, base-URL + Bearer token client, the on-demand refresh round on symbol pages (the fake
// bridge reports to the mocked engine like the real one), the data-source indicator / settings page (including Settings → Updates), main-process navigation and the
// engine-unreachable banner.
import { expect, Page, test } from "@playwright/test";
import { handle } from "./mocks";
import { STATUS } from "./mocks/live";
import * as mcp from "./mocks/mcp";
import { resetRefresh } from "./mocks/refresh";
import { resetPortfolio, settings as portfolioSettings } from "./mocks/portfolio";
import { questradeStatus } from "./mocks/market";
import { skew } from "./mocks/symbol";

const BASE = "http://127.0.0.1:59999";
const TOKEN = "per-launch-token-abcdefghijklmnop";

/** `refresh`: what refreshSymbol answers; "manual" = it stays pending until __finishRefresh(result). */
/** An idle, connected bridge (what the main process sends once a round of work is done). */
const BRIDGE_IDLE = {
  enabled: true, tws: "connected", port: 4001, endpoint: "IB Gateway (live)", accountType: "live", paused: null, lastError: null, notes: [] as string[], lastPushAt: null,
  current: null, queued: { "on-demand": 0, "eod-chains": 0, "chain-universe": 0, "underlying-stats": 0, index: 0, "option-bars": 0, "iv-history": 0 },
  backfill: { total: 0, done: 0, failed: 0 }, populate: null,
  farms: { marketData: { state: "ok", since: null, server: "usfarm" }, historical: { state: "ok", since: null, server: "ushmds" }, secDef: { state: "ok", since: null, server: "secdefil" } }, contractsAvailable: true, optionsSubscription: "unknown",
  optionsDelayed: true, lastChainAt: "2026-11-19T15:41:00-05:00", lastChainRound: { at: "2026-11-19T15:42:00-05:00", detail: "40 chains, 2 failed" },
  failures: [{ reason: "TWS error 200: No security definition has been found for the request", count: 2 }], linesPerBatch: 90,
};

async function fakeDesktop(page: Page, opts: { engineState?: "running" | "starting"; refresh?: unknown; tws?: string; update?: Record<string, unknown>; populate?: unknown; bridge?: Record<string, unknown> } = {}) {
  await page.addInitScript(({ base, token, state, refresh, tws, update, populate, bridgeOverrides }) => {
    const listeners: Record<string, ((v: unknown) => void)[]> = {};
    const on = (ch: string) => (cb: (v: unknown) => void) => { (listeners[ch] ??= []).push(cb); return () => undefined; };
    const engine = {
      state, connection: state === "running" ? { baseUrl: base, token } : null, version: "0.9.0", pid: 1, restarts: 0, lastError: null,
      dataDir: "/Users/me/Library/Application Support/OptionLens/data",
    };
    const bridge = {
      enabled: true, tws, port: 4001, endpoint: "IB Gateway (live)", accountType: "live", paused: null, lastError: null,
      notes: ["IBKR delayed (no OPRA subscription)"], lastPushAt: "2026-11-19T15:41:00-05:00", current: { kind: "iv-history", label: "AAPL IV history" },
      queued: { "on-demand": 0, "eod-chains": 0, "chain-universe": 3, "underlying-stats": 0, index: 0, "option-bars": 0, "iv-history": 12 },
      backfill: { total: 40, done: 12, failed: 1 }, populate,
      farms: { marketData: { state: "ok", since: null, server: "usfarm" }, historical: { state: "ok", since: null, server: "ushmds" }, secDef: { state: "ok", since: null, server: "secdefil" } }, contractsAvailable: true, optionsSubscription: "unknown",
      optionsDelayed: true, lastChainAt: "2026-11-19T15:41:00-05:00", lastChainRound: { at: "2026-11-19T15:42:00-05:00", detail: "40 chains, 2 failed" },
      failures: [{ reason: "TWS error 200: No security definition has been found for the request", count: 2 }], linesPerBatch: 90,
      ...bridgeOverrides,
    };
    const w = window as unknown as Record<string, unknown>;
    w.__refreshCalls = [] as string[];
    w.__updateCalls = [] as string[];
    w.__saved = [] as unknown[];
    w.__cancelRefreshCalls = [] as string[];
    w.__emit = (ch: string, v: unknown) => (listeners[ch] ?? []).forEach((cb) => cb(v));
    // "manual": every refresh waits for __finishRefresh, also when the test finishes it before the bridge's "pending"
    // report has been answered (a deferred created up front: no lost call, whatever the timing).
    let finish: (r: unknown) => void = () => undefined;
    const finished = new Promise<unknown>((resolve) => { finish = resolve; });
    w.__finishRefresh = (r: unknown) => finish(r);
    w.__startEngine = () => { (w.__emit as (c: string, v: unknown) => void)("engine", { ...engine, state: "running", connection: { baseUrl: base, token } }); };
    w.optionlens = {
      engine: async () => engine, restartEngine: async () => undefined, importData: async () => ({ ok: false, cancelled: true }),
      settings: async () => ({ bridge: { enabled: true, port: null, clientId: 17, marketDataLines: 100 }, launchAtLogin: false,
        updates: { auto: false, source: "davidl09/optionlens-mac" } }),
      saveSettings: async (s: { bridge: { clientId: number }; updates: { source: string } }) => {
        (w.__saved as unknown[]).push(s);
        if (s.bridge.clientId < 0) return { ok: false, error: "Client ID must be a whole number ≥ 0." };
        if (!/^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/.test(s.updates.source)) return { ok: false, error: "The update source must be a GitHub repository written as owner/repo." };
        return { ok: true };
      },
      bridgeStatus: async () => bridge,
      loginItemStatus: async () => "requires-approval",
      refreshSymbol: async (sym: string) => {
        (w.__refreshCalls as string[]).push(sym);
        // Like the bridge: "pending" to the engine's refresh round at once, then the outcome.
        const report = (r: { status: string; message?: string; waitingFor?: string }) => fetch(`${base}/api/ingest/refresh-result`, {
          method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ source: "ibkr", symbol: sym, status: r.status, message: r.message ?? null, waiting_for: r.waitingFor ?? null }),
        }).catch(() => undefined);
        await report({ status: "pending" });
        const r = (refresh === "manual" ? await finished : refresh) as { status: string };
        await report(r);
        return r;
      },
      cancelRefresh: async (sym: string) => { (w.__cancelRefreshCalls as string[]).push(sym); },
      cancelJobs: async () => undefined,
      updateStatus: async () => update,
      checkForUpdates: async () => { (w.__updateCalls as string[]).push("check"); },
      installUpdate: async () => { (w.__updateCalls as string[]).push("install"); return { ok: true }; },
      dismissUpdateNotice: async () => { (w.__updateCalls as string[]).push("dismiss"); },
      onEngineStatus: on("engine"), onBridgeStatus: on("bridge"), onNavigate: on("navigate"), onImportProgress: on("import-progress"), onUpdateStatus: on("update"),
      mcpSetup: async () => ({ command: "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp", args: [],
        launcher: "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp", launcherFound: true, platform: "darwin" }),
    };
  }, { base: BASE, token: TOKEN, state: opts.engineState ?? "running", refresh: opts.refresh ?? { status: "refreshed" }, tws: opts.tws ?? "connected",
    update: { ...UP_TO_DATE, ...opts.update }, populate: opts.populate ?? null, bridgeOverrides: opts.bridge ?? {} });
}

const UP_TO_DATE = {
  phase: "up-to-date", currentVersion: "0.1.3", progress: null, error: null, blocked: null, skipped: false, notice: null, checkedAt: "2026-11-21T20:00:00Z",
  latest: { version: "0.1.3", notes: "", url: "https://github.com/davidl09/optionlens-mac/releases/tag/v0.1.3", publishedAt: "2026-11-21T19:00:00Z" },
};
const emit = (page: Page, channel: string, v: unknown) => page.evaluate(([c, x]) => (window as unknown as { __emit: (c: string, v: unknown) => void }).__emit(c as string, x), [channel, v] as const);
const updateCalls = (page: Page) => page.evaluate(() => (window as unknown as { __updateCalls: string[] }).__updateCalls);

interface Seen { auth: (string | null)[]; hosts: Set<string>; unmocked: string[] }

/** `questrade`: Questrade's part of every refresh round (null: Questrade not connected). */
async function mockEngine(page: Page, failFirst = 0, questrade: Parameters<typeof resetRefresh>[0] = null): Promise<Seen> {
  resetRefresh(questrade);
  mcp.resetMcp();
  const seen: Seen = { auth: [], hosts: new Set(), unmocked: [] };
  let failures = failFirst;
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    // the app's own requests fail first (the live channel retries on its own schedule, src/live/connection.ts)
    if (failures > 0 && !url.pathname.startsWith("/api/live/")) { failures -= 1; await route.abort("connectionrefused"); return; }
    seen.hosts.add(url.origin);
    if (url.pathname !== "/api/health") seen.auth.push(req.headers()["authorization"] ?? null);
    let body: unknown = null;
    try { body = req.postDataJSON(); } catch { body = null; }
    const res = handle(req.method(), url, body);
    if (!res) { seen.unmocked.push(url.pathname); await route.fulfill({ status: 501, json: { detail: "not mocked" } }); return; }
    const headers = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Authorization, Content-Type" };
    await route.fulfill(res.text !== undefined ? { status: res.status, body: res.text, contentType: res.contentType, headers } : { status: res.status, json: res.body, headers });
  });
  return seen;
}

test("waits for the engine, then talks to it with the per-launch token", async ({ page }) => {
  await fakeDesktop(page, { engineState: "starting" });
  const seen = await mockEngine(page);
  await page.goto("/");
  await expect(page.getByText("Starting the engine…")).toBeVisible();
  await page.evaluate(() => (window as unknown as { __startEngine: () => void }).__startEngine());
  await expect(page.getByText("VIX term structure")).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect([...seen.hosts]).toEqual([BASE]);
  expect(seen.auth.length).toBeGreaterThan(0);
  expect(seen.auth.every((a) => a === `Bearer ${TOKEN}`)).toBe(true);
  expect(seen.unmocked).toEqual([]);
  await expect(page.getByRole("link", { name: "Data sources: IBKR connected · delayed data" })).toBeVisible();
  await expect(page.locator(".bridge-pill")).toContainText("IBKR connected · delayed data");   // plain words
});

test("refresh failed: the header says why and offers a retry", async ({ page }) => {
  await fakeDesktop(page, { refresh: { status: "failed", message: "TWS error 200: No security definition has been found for the request." } });
  await mockEngine(page);
  await page.goto("/s/AAPL/summary");
  const chip = page.getByRole("button", { name: /Refresh failed/ });
  await expect(chip).toBeVisible();
  await chip.click();
  const pop = page.getByRole("dialog", { name: "Refresh failed" });
  await expect(pop).toContainText("IBKR: TWS error 200: No security definition has been found for the request.");
  await pop.getByRole("button", { name: "Retry now" }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { __refreshCalls: string[] }).__refreshCalls)).toEqual(["AAPL", "AAPL"]);
});

test("opening a symbol starts a refresh round and asks the bridge for IBKR", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  await page.goto("/s/AAPL/chain");
  await expect(page.locator(".refresh-chip")).toHaveText("✓ IBKR");                   // the served chain's source
  expect(await page.evaluate(() => (window as unknown as { __refreshCalls: string[] }).__refreshCalls)).toEqual(["AAPL"]);
  // Leaving the symbol drops its refresh if it is still waiting behind other bridge work.
  await page.evaluate(() => (window as unknown as { __emit: (c: string, v: unknown) => void }).__emit("navigate", "/settings"));
  await expect(page.getByText("IB Gateway (live) · port 4001")).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __cancelRefreshCalls: string[] }).__cancelRefreshCalls)).toEqual(["AAPL"]);
});

test("no stored chain: options tabs say the chain is being fetched, then reload when the refresh lands", async ({ page }) => {
  await fakeDesktop(page, { refresh: "manual" });
  await mockEngine(page);
  let hasChain = false;
  await page.route("**/api/symbol/DELL/skew**", (route) => (hasChain
    ? route.fulfill({ json: skew("DELL", null), headers: { "Access-Control-Allow-Origin": "*" } }) : route.fallback()));
  await page.goto("/s/DELL/skew");
  await expect(page.locator(".chain-notice")).toHaveText("Fetching DELL’s option chain (IBKR)… (usually 30–90 s)");
  await expect(page.locator(".error-box")).toHaveCount(0);
  await expect(page.locator(".qh-name")).toContainText("options not loaded");        // optionable unknown: no "no options"
  await expect(page.locator(".qh-name .badge.warn")).toHaveCount(0);
  await expect(page.locator(".qh-mini")).toContainText("IV30 52.3 (Nov 20)");      // latest stored IV30, with its date
  await expect(page.locator(".qh-mini")).toContainText("IV rank 100.0%");
  await page.getByRole("link", { name: "Chain", exact: true }).click();          // every options tab, one notice
  await expect(page.locator(".chain-notice")).toContainText("Fetching DELL’s option chain (IBKR)");
  await page.getByRole("link", { name: "Volatility", exact: true }).click();     // realized vol shown; term structure waits
  await expect(page.getByText("Parkinson HV20")).toBeVisible();
  await expect(page.locator(".chain-notice")).toContainText("Fetching DELL’s option chain (IBKR)");
  await page.getByRole("link", { name: "Skew", exact: true }).click();
  hasChain = true;
  await page.evaluate(() => (window as unknown as { __finishRefresh: (r: unknown) => void }).__finishRefresh({ status: "refreshed" }));
  await expect(page.getByText(/IV smile · /)).toBeVisible();
  await expect(page.locator(".chain-notice")).toHaveCount(0);
});

test("no stored chain and IB Gateway not connected: says how to get it, and fetches once the bridge connects", async ({ page }) => {
  await fakeDesktop(page, { refresh: { status: "unavailable", message: "Not connected to IB Gateway / TWS." }, tws: "disconnected" });
  await mockEngine(page);
  await page.goto("/s/DELL/open-interest");
  await expect(page.locator(".chain-notice")).toHaveText("No option chain stored for DELL yet. Connect IB Gateway or Questrade (Settings → Data sources) to fetch it.");
  await emit(page, "bridge", BRIDGE_IDLE);
  await expect.poll(() => page.evaluate(() => (window as unknown as { __refreshCalls: string[] }).__refreshCalls)).toEqual(["DELL", "DELL"]);
});

test("no listed options: options tabs and the header say so", async ({ page }) => {
  await fakeDesktop(page, { refresh: { status: "no-options" } });
  await mockEngine(page);
  await page.goto("/s/XNOP/activity");
  await expect(page.locator(".chain-notice")).toHaveText("No listed options for XNOP.");
  await expect(page.locator(".qh-name .badge.warn")).toHaveText("no options");
  await expect(page.locator(".refresh-chip")).toHaveText("No listed options");
});

test("IBKR's contract server down and nothing saved: says so (not an error), and fetches once it is back", async ({ page }) => {
  const message = "IBKR's contract server is unavailable; will fetch automatically when it's back";
  await fakeDesktop(page, { refresh: { status: "unavailable", message, waitingFor: "contract-server" }, bridge: { contractsAvailable: false } });
  await mockEngine(page);
  await page.goto("/s/DELL/open-interest");
  await expect(page.locator(".chain-notice")).toHaveText("No option chain stored for DELL yet. IBKR’s contract server is unavailable; the chain is fetched automatically when it’s back.");
  await expect(page.locator(".refresh-chip")).toHaveText("IBKR: waiting for contract server");
  await emit(page, "bridge", BRIDGE_IDLE);                                  // 2158: the sec-def server is back
  await expect.poll(() => page.evaluate(() => (window as unknown as { __refreshCalls: string[] }).__refreshCalls)).toEqual(["DELL", "DELL"]);
});

test("no OPRA subscription outside market hours: the stored chain is shown with a neutral note, no 'refresh failed'", async ({ page }) => {
  await fakeDesktop(page, { refresh: { status: "unavailable", message: "Option quotes update during market hours (no real-time options subscription); showing the last stored chain", waitingFor: "market-hours" } });
  await mockEngine(page);
  await page.goto("/s/AAPL/chain");
  await expect(page.locator(".refresh-chip")).toHaveText("IBKR: market hours only");
  await expect(page.locator(".refresh-chip")).not.toHaveClass(/warn/);
  await expect(page.getByText("Refresh failed")).toHaveCount(0);
});

test("a chain built from the saved contract list says so", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  await page.route("**/api/symbol/AAPL/chain*", async (route) => {
    const c = handle("GET", new URL(route.request().url()), null)!.body as Record<string, unknown>;
    await route.fulfill({ json: { ...c, contracts_from_cache: true, contracts_listed_at: "2026-11-20T15:00:00Z" }, headers: { "Access-Control-Allow-Origin": "*" } });
  });
  await page.goto("/s/AAPL/chain");
  await expect(page.getByText("contract list from 2026-11-20 (IBKR contract server unavailable)")).toBeVisible();
});

test("settings shows IBKR's server farms and the options subscription", async ({ page }) => {
  await fakeDesktop(page, { bridge: {
    optionsSubscription: "delayed_only", contractsAvailable: false,
    farms: { marketData: { state: "ok", since: null, server: "usfarm" }, historical: { state: "ok", since: null, server: "ushmds" }, secDef: { state: "down", since: "2026-11-21T23:42:00Z", server: "secdefil" } },
    notes: ["IBKR sec-def server: unavailable since 18:42 (often weekend maintenance) — chains use saved contract lists", "US options: delayed only (no OPRA subscription) — option quotes are collected during market hours (09:30–16:00 ET)"],
  } });
  await mockEngine(page);
  await page.goto("/settings");
  await expect(page.getByText("market data OK · historical OK · sec-def (contracts) unavailable")).toBeVisible();
  await expect(page.getByText("delayed only (no OPRA subscription)", { exact: true })).toBeVisible();
  await expect(page.locator(".warn-box", { hasText: "IBKR sec-def server: unavailable since 18:42" })).toBeVisible();
  await expect(page.locator(".info-box", { hasText: "US options: delayed only" })).toBeVisible();
});

test("settings page shows engine, bridge status and saves bridge settings", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  await page.goto("/settings");
  await expect(page.getByText("/Users/me/Library/Application Support/OptionLens/data")).toBeVisible();
  await expect(page.getByText("IB Gateway (live) · port 4001")).toBeVisible();
  await expect(page.locator(".info-box", { hasText: "IBKR delayed (no OPRA subscription)" })).toBeVisible();   // normal delay: neutral, not a warning
  await expect(page.getByText("13 / 40 (1 failed)")).toBeVisible();
  await expect(page.locator("code.path wbr")).toHaveCount(6);                     // the data folder wraps only after a "/"
  await expect(page.getByText("macOS needs your approval")).toBeVisible();   // login item waiting for approval
  await page.getByLabel("API port").selectOption("4002");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved. The bridge reconnects")).toBeVisible();
  await page.getByLabel("Client ID").fill("-3");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Client ID must be a whole number")).toBeVisible();
});

test("Settings → Data sources: Questrade connects with a pasted token (never shown), rejects a used one, disconnects", async ({ page }) => {
  await fakeDesktop(page);
  const seen = await mockEngine(page);
  const posted: unknown[] = [];
  page.on("request", (r) => { if (r.url().endsWith("/api/sources/questrade/token") && r.method() === "POST") posted.push(r.postDataJSON()); });
  await page.goto("/settings");
  const card = page.locator("section.card", { has: page.getByRole("heading", { name: "Questrade" }) });
  await expect(card.locator(".card-sub")).toHaveText("not connected");
  await expect(card.getByRole("button", { name: "Disconnect" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Connect" })).toBeDisabled();
  const field = card.getByLabel("Questrade refresh token");
  await expect(field).toHaveAttribute("type", "password");
  await card.getByText("How to get a refresh token").click();
  await expect(card.getByText("Register a personal app")).toBeVisible();
  await field.fill("used-token");
  await card.getByRole("button", { name: "Connect" }).click();
  await expect(card.locator(".error-box, [role=alert]").first()).toContainText("Questrade refused the refresh token");
  await field.fill("  fresh-token ");
  await card.getByRole("button", { name: "Connect" }).click();
  await expect(card.locator(".card-sub")).toHaveText("connected");
  await expect(card.getByText("Token saved.")).toBeVisible();
  await expect(card.getByText("Token check: Token OK: market data permitted")).toBeVisible();
  await expect(field).toHaveValue("");                                           // cleared, never shown again
  await expect(card.getByText("real-time", { exact: true })).toBeVisible();
  await expect(card.getByText("1,234 / 10,500 calls this hour · 13,766 left at Questrade")).toBeVisible();
  await expect(card.getByText("first 8 expiries, strikes ±25%")).toBeVisible();
  await expect(card.getByText(/1140 chains/)).toBeVisible();
  await expect(page.getByText("fresh-token")).toHaveCount(0);
  expect(posted).toEqual([{ refresh_token: "used-token" }, { refresh_token: "fresh-token" }]);
  page.once("dialog", (d) => void d.accept());
  await card.getByRole("button", { name: "Disconnect" }).click();
  await expect(card.locator(".card-sub")).toHaveText("not connected");
  await expect(card.getByText("Disconnected. The token was deleted.")).toBeVisible();
  expect(seen.unmocked).toEqual([]);
});

test("Settings → Data sources: an expired Questrade token asks for a new one; Status lists the source", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  await page.route("**/api/sources/questrade", (r) => r.fulfill({ json: questradeStatus("expired"), headers: { "Access-Control-Allow-Origin": "*" } }));
  await page.goto("/settings");
  const card = page.locator("section.card", { has: page.getByRole("heading", { name: "Questrade" }) });
  await expect(card.locator(".card-sub")).toHaveText("token expired");
  await expect(card.locator(".warn-box")).toContainText("Generate a new one in Questrade's API Centre and paste it here.");
  await expect(card.getByText("Replace refresh token")).toBeVisible();
  await expect(card.getByRole("button", { name: "Disconnect" })).toBeVisible();
  await page.goto("/status");
  await expect(page.getByText("not connected (Settings → Data sources) · 0/10,500 calls this hour")).toBeVisible();
});

test("IBKR and Questrade refresh in parallel: one source-neutral chip while fetching, the served chain's badge after", async ({ page }) => {
  await fakeDesktop(page, { refresh: "manual" });
  await mockEngine(page, 0, { state: "ok" });
  await page.goto("/s/AAPL/chain");
  await expect(page.locator(".refresh-chip")).toHaveText("Fetching chain (IBKR, Questrade)…");
  await page.evaluate(() => (window as unknown as { __finishRefresh: (r: unknown) => void }).__finishRefresh({ status: "failed", message: "TWS error 200" }));
  await expect(page.locator(".refresh-chip")).toHaveText("✓ Questrade");             // IBKR's failure is not shown
  await expect(page.getByText("Refresh failed")).toHaveCount(0);
});

test("every source failed: the reasons are named per source", async ({ page }) => {
  await fakeDesktop(page, { refresh: { status: "unavailable", message: "IBKR's contract server is unavailable; will fetch automatically when it's back", waitingFor: "contract-server" } });
  await mockEngine(page, 0, { state: "failed", reason: "403 code 1016 (missing market-data permission)",
    message: "Your Questrade app lacks the 'Retrieve delayed and real-time market data' permission. In Questrade's API Centre enable it for your personal app, generate a new manual token, and paste it here." });
  await page.goto("/s/DELL/open-interest");
  await expect(page.locator(".chain-notice")).toContainText("Could not fetch DELL’s option chain: IBKR contract server unavailable; Questrade: missing market-data permission.");
  await page.getByRole("button", { name: /Refresh failed/ }).click();
  await expect(page.getByRole("dialog", { name: "Refresh failed" })).toContainText("Questrade: Your Questrade app lacks the 'Retrieve delayed and real-time market data' permission.");
});

test("Settings → Data sources: IBKR and Questrade side by side with the same fields; a missing permission is explained", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  await page.route("**/api/sources/questrade", (r) => r.fulfill({ json: questradeStatus("needs_attention"), headers: { "Access-Control-Allow-Origin": "*" } }));
  await page.goto("/settings");
  const ib = page.locator("section.card", { has: page.getByRole("heading", { name: "IBKR", exact: true }) });
  const qt = page.locator("section.card", { has: page.getByRole("heading", { name: "Questrade" }) });
  for (const label of ["State", "Data", "Last chain", "Last sweep", "Failures (24 h)", "Usage"]) {
    await expect(ib.locator("dt", { hasText: label })).toHaveCount(1);
    await expect(qt.locator("dt", { hasText: label })).toHaveCount(1);
  }
  await expect(qt.locator(".card-sub")).toHaveText("needs attention");
  await expect(qt.locator(".warn-box")).toContainText("lacks the 'Retrieve delayed and real-time market data' permission");
  await expect(qt.getByText("1,141 × 403 code 1016 (missing market-data permission)")).toBeVisible();
  await expect(ib.getByText("2 × TWS error 200: No security definition has been found for the request")).toBeVisible();
  await expect(ib.getByText("40 chains, 2 failed")).toBeVisible();
  await expect(page.getByText(/IBKR and Questrade are equal sources/)).toBeVisible();
  await expect(page.locator(".sources-pill")).toContainText("Questrade needs attention");
});

test("populating the last session after hours: progress in Settings and Status, and empty views say so", async ({ page }) => {
  await fakeDesktop(page, { populate: { sessionDate: "2026-11-20", done: 340, total: 1145 } });
  await mockEngine(page);
  await page.route("**/api/market/overview", async (route) => {
    const m = handle("GET", new URL(route.request().url()), null)!.body as Record<string, unknown>;
    await route.fulfill({ json: { ...m, unusual_options: [], empty_notes: { unusual_options: "No option volume collected yet." } }, headers: { "Access-Control-Allow-Origin": "*" } });
  });
  await page.goto("/settings");
  await expect(page.locator(".progress-label", { hasText: "Populating last session's data: 340/1,145" })).toBeVisible();
  await page.goto("/status");
  await expect(page.locator(".bridge-summary")).toContainText("Populating last session's data: 340/1,145");
  await page.goto("/");
  await expect(page.locator(".panel-notice")).toContainText("No option volume collected yet. Populating last session's data: 340/1,145.");
  await page.goto("/reports/oi-changes");                                  // an empty report
  await expect(page.locator(".dt-empty")).toContainText("Populating last session's data: 340/1,145.");
  await emit(page, "bridge", BRIDGE_IDLE);            // done: the note goes away
  await expect(page.locator(".dt-empty")).not.toContainText("Populating");
});

test("main-process navigation (notification click) opens the link in-app", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  await page.goto("/");
  await expect(page.getByText("VIX term structure")).toBeVisible();
  await page.evaluate(() => (window as unknown as { __emit: (c: string, v: unknown) => void }).__emit("navigate", "/s/MSFT/summary"));
  await expect(page).toHaveURL(/\/s\/MSFT\/summary/);
});

test("engine unreachable: banner with retry, then recovery reloads the page", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page, 4);                  // every request of the first load (incl. the header's Questrade status)
  await page.goto("/status");
  await expect(page.getByRole("alert").filter({ hasText: "Can't reach the OptionLens engine" })).toBeVisible();
  await page.getByRole("button", { name: "Retry" }).first().click();
  await expect(page.getByText("Job runs")).toBeVisible();
  await expect(page.locator(".conn-banner")).toHaveCount(0);
});

test("Settings → Data sources: \"Show my positions\" per broker (on by default); MCP portfolio access off by default", async ({ page }) => {
  resetPortfolio();
  await fakeDesktop(page);
  const seen = await mockEngine(page);
  await page.goto("/settings");
  const qt = page.locator("section.card").filter({ has: page.getByRole("heading", { name: "Questrade", exact: true }) });
  const ib = page.locator("section.card").filter({ has: page.getByRole("heading", { name: "IBKR", exact: true }) });
  const qSwitch = qt.getByRole("switch", { name: "Show my positions" });
  const iSwitch = ib.getByRole("switch", { name: "Show my positions" });
  await expect(qSwitch).toHaveAttribute("aria-checked", "true");
  await expect(iSwitch).toHaveAttribute("aria-checked", "true");
  await expect(ib.getByText("“Read-Only API” setting can stay on")).toBeVisible();
  await iSwitch.click();
  await expect(iSwitch).toHaveAttribute("aria-checked", "false");
  await expect(ib.getByText("Off: hidden, not fetched")).toBeVisible();
  expect(portfolioSettings()).toEqual({ questrade: true, ibkr: false });
  await expect(qt.getByText("Retrieve balances, positions, orders and executions")).toBeAttached();   // the token steps name it
  const card = page.locator("section.card").filter({ has: page.getByRole("heading", { name: "AI agents (MCP)" }) });
  await expect(card.getByRole("switch", { name: "Allow agents to see my portfolio" })).toHaveCount(0);   // only while access is on
  await card.getByRole("switch", { name: "Allow AI agents (MCP)" }).click();
  const pf = card.getByRole("switch", { name: "Allow agents to see my portfolio" });
  await expect(pf).toHaveAttribute("aria-checked", "false");
  await pf.click();
  await expect(pf).toHaveAttribute("aria-checked", "true");
  await card.getByRole("switch", { name: "Allow AI agents (MCP)" }).click();
  await expect(pf).toHaveCount(0);
  expect(mcp.status().portfolio).toBe(false);                                         // off resets it
  expect(seen.unmocked).toEqual([]);
});

test("Settings → Updates: up to date, available (notes as plain text), downloading %, ready, error", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  await page.goto("/settings");
  const card = page.locator(".updates");
  await expect(card).toHaveAttribute("data-phase", "up-to-date");
  await expect(card.getByText("OptionLens is up to date.")).toBeVisible();
  await expect(card.getByRole("button", { name: "Download and install" })).toHaveCount(0);
  await card.getByRole("button", { name: "Check now" }).click();
  await expect.poll(() => updateCalls(page)).toEqual(["check"]);

  const latest = { version: "0.1.4", notes: "## Fixes\n<b>bold</b> <img src=x onerror=\"window.__xss=1\">", url: "https://github.com/x", publishedAt: null };
  await emit(page, "update", { ...UP_TO_DATE, phase: "available", latest });
  await expect(card.getByText("Version 0.1.4 is available.")).toBeVisible();
  await expect(card.locator(".release-notes pre")).toHaveText(latest.notes);             // shown as text
  await expect(card.locator(".release-notes b, .release-notes img")).toHaveCount(0);       // never as HTML
  expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
  await card.getByRole("button", { name: "Download and install" }).click();
  await expect.poll(() => updateCalls(page)).toEqual(["check", "install"]);

  await emit(page, "update", { ...UP_TO_DATE, phase: "downloading", latest, progress: 0.42 });
  await expect(card.getByText("Downloading version 0.1.4…")).toBeVisible();
  await expect(card.getByRole("progressbar", { name: "Update download" })).toHaveAttribute("aria-valuenow", "42");
  await expect(card.getByText("42%")).toBeVisible();
  await expect(card.getByRole("button", { name: "Check now" })).toBeDisabled();

  await emit(page, "update", { ...UP_TO_DATE, phase: "ready", latest, blocked: "Waiting until the computer is idle to restart into the update." });
  await expect(card.getByText("Version 0.1.4 is downloaded and verified.")).toBeVisible();
  await expect(card.locator(".warn-box", { hasText: "Waiting until the computer is idle" })).toBeVisible();
  await card.getByRole("button", { name: "Restart and install" }).click();
  await expect.poll(() => updateCalls(page)).toEqual(["check", "install", "install"]);

  await emit(page, "update", { ...UP_TO_DATE, phase: "error", latest, error: "The download's SHA-256 (x) does not match the release (y); it was deleted." });
  await expect(card.getByRole("alert")).toContainText("does not match the release");
  await expect(card.getByRole("button", { name: "Download and install" })).toBeVisible();     // retry
});

test("Settings → Updates: move-to-Applications, rollback notice, automatic mode and the update source", async ({ page }) => {
  await fakeDesktop(page, { update: { phase: "available", blocked: "Move OptionLens to the Applications folder first (then open it from there), so it can update itself.",
    notice: "OptionLens 0.1.4 did not start correctly, so 0.1.3 was restored. 0.1.4 will not be installed automatically.", skipped: true,
    latest: { version: "0.1.4", notes: "", url: "https://github.com/x", publishedAt: null } } });
  await mockEngine(page);
  await page.goto("/settings");
  const card = page.locator(".updates");
  await expect(card.getByText("Move OptionLens to the Applications folder first")).toBeVisible();
  await expect(card.getByText("This version was rolled back on this computer")).toBeVisible();
  await card.getByRole("button", { name: "Dismiss" }).click();
  await expect.poll(() => updateCalls(page)).toEqual(["dismiss"]);

  await card.getByLabel("Install updates automatically").check();
  const saved = () => page.evaluate(() => (window as unknown as { __saved: { updates: unknown }[] }).__saved.map((s) => s.updates));
  await expect.poll(saved).toEqual([{ auto: true, source: "davidl09/optionlens-mac" }]);
  await card.getByLabel("Update source").fill("https://github.com/x/y");
  await card.getByRole("button", { name: "Save source" }).click();
  await expect(card.getByRole("alert")).toContainText("owner/repo");
  await card.getByLabel("Update source").fill("someone/fork");
  await card.getByRole("button", { name: "Save source" }).click();
  await expect(card.getByText("Saved. OptionLens checks this repository for updates.")).toBeVisible();
});

test("settings: the stream switch toggles the live mode, with Questrade's warning and the live status", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  let mode: "poll" | "stream" = "poll";
  const puts: unknown[] = [];
  await page.route("**/api/live/mode", async (route) => {
    if (route.request().method() === "PUT") { puts.push(route.request().postDataJSON()); mode = (route.request().postDataJSON() as { mode: "poll" | "stream" }).mode; }
    await route.fulfill({ json: { mode, default: "poll", saved: puts.length > 0 } });
  });
  await page.route("**/api/live/status", (route) => route.fulfill({ json: {
    ...STATUS.live, mode, interval_s: mode === "poll" ? 2 : null, fallback: null,
    questrade: { setting: mode, streaming: mode === "stream", fallback: null, fallback_detail: null, retry_at: null, messages_per_s: 4.2,
      last_message_at: "2026-11-24T15:00:00Z", drops_recent: 0,
      streams: mode === "stream" ? [{ ids: { equity: 3, option: 42 }, open: true, port: 1, opened_at: null, last_message_at: null, messages: 14, connects: 1 }] : [] },
  } }));
  await page.goto("/settings");
  const sw = page.getByRole("switch", { name: "Stream live quotes (tick-by-tick)" });
  await expect(sw).toHaveAttribute("aria-checked", "false");
  await expect(page.getByText(/While OptionLens streams, market data in other Questrade platforms you have open \(e\.g\. Questrade Edge\) may freeze/)).toBeVisible();
  const status = page.getByTestId("live-mode-status");
  await expect(status).toHaveText("Polling: snapshots every 2.0 s.");
  await sw.focus();
  await page.keyboard.press("Space");
  await expect(sw).toHaveAttribute("aria-checked", "true");
  await expect(status).toHaveText(/^Streaming connected: 3 symbols, 42 options · 4\.2 msg\/s · last message /);
  await page.keyboard.press("Enter");
  await expect(sw).toHaveAttribute("aria-checked", "false");
  await expect(status).toHaveText("Polling: snapshots every 2.0 s.");
  expect(puts).toEqual([{ mode: "stream" }, { mode: "poll" }]);
});

test("Settings → AI agents (MCP): off by default; on shows the warning, who is connected and setup snippets; regenerate; off", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await fakeDesktop(page);
  const seen = await mockEngine(page);
  await page.goto("/settings");
  const card = page.locator("section.card").filter({ has: page.getByRole("heading", { name: "AI agents (MCP)" }) });
  const sw = card.getByRole("switch", { name: "Allow AI agents (MCP)" });
  await expect(sw).toHaveAttribute("aria-checked", "false");
  await expect(card.getByTestId("mcp-status")).toHaveText("Off: no agent can connect.");
  await expect(card.getByText("Agents with this access can read all your OptionLens data (your portfolio only if you also allow it below) and edit watchlists/alerts/presets. No trading is possible.")).toBeVisible();
  const claudeCode = 'claude mcp add optionlens -- "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp"';
  await expect(card.getByLabel("Claude Code", { exact: true })).toHaveText(claudeCode);
  await expect(card.getByLabel("Claude Desktop", { exact: true })).toContainText('"command": "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp"');
  await expect(card.getByText("add to ~/Library/Application Support/Claude/claude_desktop_config.json")).toBeVisible();
  await expect(card.getByLabel("Any MCP client (stdio command)", { exact: true })).toHaveText('"/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp"');
  await card.getByRole("button", { name: "Copy Claude Code" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(claudeCode);
  await expect(card.getByRole("button", { name: "Copy Claude Code" })).toHaveText("Copied");
  await expect(card.getByRole("button", { name: "Regenerate key" })).toHaveCount(0);

  await sw.click();
  await expect(sw).toHaveAttribute("aria-checked", "true");
  await expect(card.getByTestId("mcp-status")).toContainText("On · connected: claude-code/2.1.0");
  await expect(card.getByTestId("mcp-status")).toContainText("Last call: GET /api/symbol/{sym}/summary by claude-code/2.1.0");
  await expect(card.getByText("mcp-connection.json")).toBeVisible();
  page.once("dialog", (d) => void d.accept());
  await card.getByRole("button", { name: "Regenerate key" }).click();
  await expect.poll(() => mcp.regenerations()).toBe(1);
  await sw.click();
  await expect(sw).toHaveAttribute("aria-checked", "false");
  expect(seen.unmocked).toEqual([]);
});

test("the current view is reported for agents: route, report, table state, visible rows, page details", async ({ page }) => {
  await fakeDesktop(page);
  const seen = await mockEngine(page);
  await page.goto("/reports/stock-screener");
  await expect(page.getByText("600 rows")).toBeVisible();
  const last = () => mcp.views.at(-1);
  await expect.poll(() => last()?.report_id).toBe("stock-screener");
  const reportData = () => last()?.data.find((d) => d.path.startsWith("/api/reports/stock-screener"));
  await expect.poll(() => reportData()?.rows).toEqual({ offset: 0, limit: 100, total: 600 });
  expect(last()).toMatchObject({ route: "/reports/stock-screener", symbol: null, tab: null, title: "Stock screener" });
  expect(reportData()?.method).toBe("GET");
  await page.locator("th").filter({ hasText: "Price" }).locator(".th-sort").click();
  await expect.poll(() => reportData()?.table?.sort).toEqual({ key: "price", dir: "desc" });
  await page.getByRole("button", { name: "Filter IV30" }).click();
  await page.getByLabel("IV30 minimum").fill("50");
  await expect.poll(() => reportData()?.table?.filters?.iv30?.min).toBe(0.5);
  await expect.poll(() => (reportData()?.rows?.total ?? 600) < 600).toBe(true);
  await page.keyboard.press("Escape");

  await page.goto("/s/AAPL/chain");
  await expect.poll(() => last()?.symbol).toBe("AAPL");
  expect(last()).toMatchObject({ tab: "chain", route: "/s/AAPL/chain" });
  await expect.poll(() => typeof last()?.details.expiry).toBe("string");
  expect(last()?.details).toMatchObject({ side: "both", strikes_around_atm: "10", greeks: false });
  const paths = last()?.data.map((d) => d.path) ?? [];
  expect(paths).toContain("/api/symbol/AAPL/chain");
  expect(paths).toContain("/api/symbol/AAPL/summary");                 // the quote header is on screen too
  expect(paths.some((p) => p.startsWith("/api/notifications"))).toBe(false);  // the header bell is not the page
  expect(seen.unmocked).toEqual([]);
});
