// Desktop-mode smoke: a fake preload API (window.optionlens) stands in for the Electron main process.
// Checks the engine gate, base-URL + Bearer token client, IBKR refresh on symbol pages, the bridge
// indicator / settings page, main-process navigation and the engine-unreachable banner.
import { expect, Page, test } from "@playwright/test";
import { handle } from "./mocks";
import { skew } from "./mocks/symbol";

const BASE = "http://127.0.0.1:59999";
const TOKEN = "per-launch-token-abcdefghijklmnop";

/** `refresh`: what refreshSymbol answers; "manual" = it stays pending until __finishRefresh(result). */
async function fakeDesktop(page: Page, opts: { engineState?: "running" | "starting"; refresh?: unknown; tws?: string } = {}) {
  await page.addInitScript(({ base, token, state, refresh, tws }) => {
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
      backfill: { total: 40, done: 12, failed: 1 },
    };
    const w = window as unknown as Record<string, unknown>;
    w.__refreshCalls = [] as string[];
    w.__cancelRefreshCalls = [] as string[];
    w.__emit = (ch: string, v: unknown) => (listeners[ch] ?? []).forEach((cb) => cb(v));
    let finish: ((r: unknown) => void) | null = null;
    w.__finishRefresh = (r: unknown) => finish?.(r);
    w.__startEngine = () => { (w.__emit as (c: string, v: unknown) => void)("engine", { ...engine, state: "running", connection: { baseUrl: base, token } }); };
    w.optionlens = {
      engine: async () => engine, restartEngine: async () => undefined, importData: async () => ({ ok: false, cancelled: true }),
      settings: async () => ({ bridge: { enabled: true, port: null, clientId: 17, marketDataLines: 100 }, launchAtLogin: false }),
      saveSettings: async (s: { bridge: { clientId: number } }) => (s.bridge.clientId >= 0 ? { ok: true } : { ok: false, error: "Client ID must be a whole number ≥ 0." }),
      bridgeStatus: async () => bridge,
      loginItemStatus: async () => "requires-approval",
      refreshSymbol: async (sym: string) => {
        (w.__refreshCalls as string[]).push(sym);
        return refresh === "manual" ? new Promise((resolve) => { finish = resolve; }) : refresh;
      },
      cancelRefresh: async (sym: string) => { (w.__cancelRefreshCalls as string[]).push(sym); },
      cancelJobs: async () => undefined,
      onEngineStatus: on("engine"), onBridgeStatus: on("bridge"), onNavigate: on("navigate"), onImportProgress: on("import-progress"),
    };
  }, { base: BASE, token: TOKEN, state: opts.engineState ?? "running", refresh: opts.refresh ?? { status: "refreshed" }, tws: opts.tws ?? "connected" });
}

interface Seen { auth: (string | null)[]; hosts: Set<string>; unmocked: string[] }

async function mockEngine(page: Page, failFirst = 0): Promise<Seen> {
  const seen: Seen = { auth: [], hosts: new Set(), unmocked: [] };
  let failures = failFirst;
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (failures > 0) { failures -= 1; await route.abort("connectionrefused"); return; }
    seen.hosts.add(url.origin);
    if (url.pathname !== "/api/health") seen.auth.push(req.headers()["authorization"] ?? null);
    let body: unknown = null;
    try { body = req.postDataJSON(); } catch { body = null; }
    const res = handle(req.method(), url, body);
    if (!res) { seen.unmocked.push(url.pathname); await route.fulfill({ status: 501, json: { detail: "not mocked" } }); return; }
    await route.fulfill({ status: res.status, json: res.body, headers: { "Access-Control-Allow-Origin": "*" } });
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
  await expect(page.getByRole("link", { name: /IBKR bridge: IB Gateway \(live\) connected/ })).toBeVisible();
});

test("opening a symbol asks the bridge for an IBKR refresh", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  await page.goto("/s/AAPL/chain");
  await expect(page.locator(".refresh-chip")).toHaveText("IBKR ✓");
  expect(await page.evaluate(() => (window as unknown as { __refreshCalls: string[] }).__refreshCalls)).toEqual(["AAPL"]);
  // Leaving the symbol drops its refresh if it is still waiting behind other bridge work.
  await page.evaluate(() => (window as unknown as { __emit: (c: string, v: unknown) => void }).__emit("navigate", "/settings"));
  await expect(page.getByText("IB Gateway (live) · port 4001")).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __cancelRefreshCalls: string[] }).__cancelRefreshCalls)).toEqual(["AAPL"]);
});

test("no stored chain: options tabs say the chain is being fetched, then reload when the IBKR refresh lands", async ({ page }) => {
  await fakeDesktop(page, { refresh: "manual" });
  await mockEngine(page);
  let hasChain = false;
  await page.route("**/api/symbol/DELL/skew**", (route) => (hasChain
    ? route.fulfill({ json: skew("DELL", null), headers: { "Access-Control-Allow-Origin": "*" } }) : route.fallback()));
  await page.goto("/s/DELL/skew");
  await expect(page.locator(".chain-notice")).toHaveText("Fetching DELL’s option chain from IBKR… (usually 30–90 s)");
  await expect(page.locator(".error-box")).toHaveCount(0);
  await expect(page.locator(".qh-name")).toContainText("options not loaded");        // optionable unknown: no "no options"
  await expect(page.locator(".qh-name .badge.warn")).toHaveCount(0);
  await expect(page.locator(".qh-mini")).toContainText("IV30 52.3 (Nov 20)");      // latest stored IV30, with its date
  await expect(page.locator(".qh-mini")).toContainText("IV rank 100.0%");
  await page.getByRole("link", { name: "Chain", exact: true }).click();          // every options tab, one notice
  await expect(page.locator(".chain-notice")).toContainText("Fetching DELL’s option chain from IBKR");
  await page.getByRole("link", { name: "Volatility", exact: true }).click();     // realized vol shown; term structure waits
  await expect(page.getByText("Parkinson HV20")).toBeVisible();
  await expect(page.locator(".chain-notice")).toContainText("Fetching DELL’s option chain from IBKR");
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
  await expect(page.locator(".chain-notice")).toHaveText("No option chain stored for DELL yet. Connect IB Gateway to fetch it.");
  await page.evaluate(() => (window as unknown as { __emit: (c: string, v: unknown) => void }).__emit("bridge", {
    enabled: true, tws: "connected", port: 4001, endpoint: "IB Gateway (live)", accountType: "live", paused: null, lastError: null, notes: [], lastPushAt: null,
    current: null, queued: { "on-demand": 0, "eod-chains": 0, "chain-universe": 0, "underlying-stats": 0, index: 0, "option-bars": 0, "iv-history": 0 },
    backfill: { total: 0, done: 0, failed: 0 },
  }));
  await expect.poll(() => page.evaluate(() => (window as unknown as { __refreshCalls: string[] }).__refreshCalls)).toEqual(["DELL", "DELL"]);
});

test("IBKR lists no options: options tabs and the header say so", async ({ page }) => {
  await fakeDesktop(page, { refresh: { status: "no-options" } });
  await mockEngine(page);
  await page.goto("/s/XNOP/activity");
  await expect(page.locator(".chain-notice")).toHaveText("IBKR lists no options for XNOP.");
  await expect(page.locator(".qh-name .badge.warn")).toHaveText("no options");
  await expect(page.locator(".refresh-chip")).toHaveText("IBKR: no options");
});

test("settings page shows engine, bridge status and saves bridge settings", async ({ page }) => {
  await fakeDesktop(page);
  await mockEngine(page);
  await page.goto("/settings");
  await expect(page.getByText("/Users/me/Library/Application Support/OptionLens/data")).toBeVisible();
  await expect(page.getByText("IB Gateway (live) · port 4001")).toBeVisible();
  await expect(page.locator(".warn-box", { hasText: "IBKR delayed (no OPRA subscription)" })).toBeVisible();
  await expect(page.getByText("13/40 (1 failed)")).toBeVisible();
  await expect(page.getByText("macOS needs your approval")).toBeVisible();   // login item waiting for approval
  await page.getByLabel("API port").selectOption("4002");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved. The bridge reconnects")).toBeVisible();
  await page.getByLabel("Client ID").fill("-3");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Client ID must be a whole number")).toBeVisible();
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
  await mockEngine(page, 3);
  await page.goto("/status");
  await expect(page.getByRole("alert").filter({ hasText: "Can't reach the OptionLens engine" })).toBeVisible();
  await page.getByRole("button", { name: "Retry" }).first().click();
  await expect(page.getByText("Job runs")).toBeVisible();
  await expect(page.locator(".conn-banner")).toHaveCount(0);
});
