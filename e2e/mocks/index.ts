// Routes a mocked /api request to its fixture. Returns null for unknown endpoints (test fails loudly).
import type { CalcRequest, IntradayInterval, IntradayRange, Watchlist } from "../../src/api/types";
import * as live from "./live";
import * as mcp from "./mcp";
import * as m from "./market";
import * as refresh from "./refresh";
import { reportResult, REPORT_DEFS } from "./reports";
import * as s from "./symbol";

/** `text` (with `contentType`): a non-JSON body, e.g. the live channel's text/event-stream. */
export interface MockResponse { status: number; body: unknown; text?: string; contentType?: string }

const ok = (body: unknown): MockResponse => ({ status: 200, body });
/** Endpoints that need a stored chain (404 + code for the NO_CHAIN symbols). */
const CHAIN_ENDPOINTS = new Set(["chain", "activity", "open-interest", "contract", "skew", "implied-moves", "straddle", "ideas"]);

export function handle(method: string, url: URL, body: unknown): MockResponse | null {
  const p = url.pathname;
  const q = url.searchParams;
  const parts = p.split("/").filter(Boolean); // ["api", ...]
  const syms = (q.get("symbols") ?? "").split(",").filter(Boolean);

  if (p === "/api/health") return ok({ ok: true });
  if (p === "/api/view") return ok(method === "PUT" ? mcp.reportView(body as Parameters<typeof mcp.reportView>[0]) : mcp.currentView());
  if (p === "/api/mcp") return ok(method === "PUT" ? mcp.set((body as { enabled: boolean }).enabled) : mcp.status());
  if (p === "/api/mcp/regenerate" && method === "POST") { const r = mcp.regenerate(); return r ? ok(r) : { status: 409, body: { detail: "MCP access is off" } }; }
  if (p === "/api/live/stream") return { status: 200, body: null, text: live.defaultStream(), contentType: "text/event-stream" };
  if (p === "/api/live/subscribe" && method === "POST") return ok(live.subscribed(body as Parameters<typeof live.subscribed>[0]));
  if (p === "/api/live/status") return ok({ ...live.STATUS.closed, questrade: null });
  if (p === "/api/live/mode") return ok({ mode: method === "PUT" ? (body as { mode?: string } | null)?.mode ?? "poll" : "poll", default: "poll", saved: method === "PUT" });
  if (p === "/api/ingest/refresh-result" && method === "POST") return ok(refresh.report(body as Parameters<typeof refresh.report>[0]));
  if (p === "/api/status") return ok(m.status());
  if (p === "/api/sources/questrade") return ok(m.questradeStatus());
  if (p === "/api/sources/questrade/token") {
    if (method === "DELETE") return ok(m.questradeStatus("disconnected"));
    const token = (body as { refresh_token?: string } | null)?.refresh_token ?? "";
    return token === "used-token"
      ? { status: 422, body: { detail: "Questrade refused the refresh token (it expired, was already used or was revoked). Generate a new one in Questrade's API Centre and paste it here.", code: "token_rejected" } }
      : ok(m.questradeStatus("connected"));
  }
  if (p === "/api/search") return ok(m.search(q.get("q") ?? ""));
  if (p === "/api/market/overview") return ok(m.overview());
  if (p === "/api/strategy-templates") return ok(m.TEMPLATES);
  if (p === "/api/calc/strategy" && method === "POST") return ok(m.calc(body as CalcRequest));
  if (p === "/api/reports") return ok(REPORT_DEFS);
  if (parts[1] === "reports" && parts[2]) {
    const r = reportResult(parts[2], Object.fromEntries(q.entries()));
    return r ? ok(r) : { status: 404, body: { detail: "Unknown report" } };
  }
  if (p === "/api/backtest/stock" || p === "/api/backtest/options") return ok(s.backtest("SPY"));
  if (p === "/api/compare/volatility") return ok(m.compareVolatility(syms));
  if (p === "/api/compare/straddle") return ok(m.compareStraddle(syms, Number(q.get("dte") ?? 30)));

  if (parts[1] === "watchlists") {
    if (parts.length === 2) {
      if (method === "POST") { const b = body as { name: string; symbols?: string[] }; return ok({ id: 99, name: b.name, symbols: b.symbols ?? [], created_at: "2026-11-19T16:00:00-05:00" } satisfies Watchlist); }
      return ok(m.WATCHLISTS);
    }
    const id = Number(parts[2]);
    if (parts[3] === "table") return ok(m.watchlistTable(id));
    return ok(m.WATCHLISTS.find((w) => w.id === id) ?? m.WATCHLISTS[0]);
  }
  if (parts[1] === "presets") {
    if (method === "POST") return ok({ ...(body as object), id: 50, created_at: "2026-11-19T16:00:00-05:00" });
    if (method === "DELETE") return ok({ ok: true });
    return ok(m.PRESETS.filter((x) => x.report_id === q.get("report_id")));
  }
  if (parts[1] === "alerts") {
    if (method === "GET") return ok(m.ALERTS);
    if (method === "DELETE") return ok({ ok: true });
    return ok({ ...m.ALERTS[0], ...(body as object), id: parts[2] ? Number(parts[2]) : 77 });
  }
  if (parts[1] === "notifications") {
    if (parts[2] === "read") return ok({ ok: true });
    return ok(q.get("unread") ? m.NOTIFICATIONS.filter((n) => !n.read) : m.NOTIFICATIONS);
  }

  if (parts[1] === "symbol" && parts[2]) {
    const sym = parts[2].toUpperCase();
    const exp = q.get("expiry");
    const tail = parts.slice(3).join("/");
    const lookback = Number(q.get("lookback") ?? 12), entry = Number(q.get("entry") ?? -1), exit = Number(q.get("exit") ?? 1);
    if (sym === "NOPE") return { status: 404, body: { detail: "Unknown symbol NOPE" } };
    if (parts[3] === "refresh") return ok(method === "POST" ? refresh.start(sym) : refresh.status(sym));
    const noChain = s.NO_CHAIN[sym];
    if (noChain && CHAIN_ENDPOINTS.has(parts[3])) {
      return { status: 404, body: { detail: noChain === "no_options" ? `${sym} lists no options` : `no option chain for ${sym}`, code: noChain } };
    }
    switch (parts[3]) {
      case "summary": return ok(s.summary(sym));
      case "key-stats": return ok(s.keyStats(sym));
      case "bars":
        if (q.get("interval")) return ok(s.intradayBars(sym, q.get("interval") as IntradayInterval, (q.get("range") ?? "1d") as IntradayRange, q.get("extended") === "1"));
        return ok(s.bars(sym, q.get("range") ?? "1y"));
      case "price-action": return ok(s.priceAction());
      case "chain": return ok(s.chain(sym, exp));
      case "activity": return ok(s.activity(sym, exp));
      case "open-interest": return ok(s.openInterest(sym, exp));
      case "contract": return ok(s.contract(sym, decodeURIComponent(parts[4] ?? "")));
      case "volatility": return ok(s.volatility(sym));
      case "skew": return ok(s.skew(sym, exp));
      case "implied-moves": return ok(s.impliedMoves(sym));
      case "vol-seasonality": return ok(s.volSeasonality(sym));
      case "earnings":
        if (tail === "earnings") return ok(s.earnings(sym));
        if (parts[6] === "trades") return ok(s.trades(sym, lookback));
        return ok(s.earningsStrategies(sym, lookback, entry, exit));
      case "dividends": return ok(s.dividends(sym));
      case "seasonality": return ok(s.seasonality(sym));
      case "events": return ok(s.events(sym));
      case "straddle": return ok(s.straddle(sym));
      case "ideas": return ok(s.ideas(sym));
      case "fundamentals": return ok(s.fundamentals(sym, q.get("period") === "quarterly" ? "quarterly" : "annual"));
      case "peers": return ok(s.peers(sym));
      case "insiders": return ok(s.insiders(sym));
      case "news": return ok(s.news(sym));
      case "profile": return ok(s.profile(sym));
      default: return null;
    }
  }
  return null;
}
