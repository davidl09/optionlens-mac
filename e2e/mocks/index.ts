// Routes a mocked /api request to its fixture. Returns null for unknown endpoints (test fails loudly).
import type { CalcRequest, Watchlist } from "../../src/api/types";
import * as m from "./market";
import { reportResult, REPORT_DEFS } from "./reports";
import * as s from "./symbol";

export interface MockResponse { status: number; body: unknown }

const ok = (body: unknown): MockResponse => ({ status: 200, body });

export function handle(method: string, url: URL, body: unknown): MockResponse | null {
  const p = url.pathname;
  const q = url.searchParams;
  const parts = p.split("/").filter(Boolean); // ["api", ...]
  const syms = (q.get("symbols") ?? "").split(",").filter(Boolean);

  if (p === "/api/health") return ok({ ok: true });
  if (p === "/api/status") return ok(m.status());
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
    switch (parts[3]) {
      case "summary": return ok(s.summary(sym));
      case "bars": return ok(s.bars(q.get("range") ?? "1y"));
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
        if (parts[5] === "trades") return ok(s.trades(sym, lookback));
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
