// Global header: brand, nav menus (report entries come from GET /api/reports), search, bell, theme.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api } from "../api/client";
import type { ReportDef } from "../api/types";
import { useApi } from "../api/useApi";
import { useTheme } from "../state/theme";
import { BridgeIndicator } from "./Desktop";
import { NotificationBell } from "./NotificationBell";
import { SymbolSearch } from "./SymbolSearch";
import { useClickOutside } from "./ui";

export const INDEX_SYMBOLS = "SPX,NDX,RUT,XSP";

interface MenuItem { to: string; label: string; title?: string }
interface MenuSection { title?: string; items: MenuItem[] }
interface Menu { label: string; sections: MenuSection[] }

const reportItem = (d: ReportDef): MenuItem => ({ to: `/reports/${d.id}`, label: d.title, title: d.description });

export function buildMenus(defs: ReportDef[]): Menu[] {
  const byCat = (c: ReportDef["category"]) => defs.filter((d) => d.category === c).map(reportItem);
  const known = new Set(defs.map((d) => d.id));
  const fixedReports = (ids: [string, string][]) => ids.filter(([id]) => known.has(id)).map(([id, label]) => ({ to: `/reports/${id}`, label }));
  return [
    { label: "Market", sections: [
      { items: [{ to: "/", label: "Dashboard" }, { to: "/reports", label: "All reports" }] },
      { title: "Snapshot", items: fixedReports([["market-movers", "Market movers"], ["performance-by-sector", "Sector performance"], ["options-broad-view", "Options broad view"], ["earnings-upcoming-summary", "Earnings week ahead"]]) },
    ] },
    { label: "Options", sections: [{ items: byCat("Options") }] },
    { label: "Strategies", sections: [
      { items: byCat("Strategies") },
      { title: "Indices", items: [{ to: `/reports?category=Strategies&symbols=${INDEX_SYMBOLS}`, label: "Index options strategies", title: "Strategy screeners on SPX, NDX, RUT, XSP" }] },
    ] },
    { label: "Earnings", sections: [{ items: byCat("Earnings") }] },
    { label: "Volatility", sections: [
      { items: byCat("Volatility") },
      { title: "Compare", items: [{ to: "/compare/volatility", label: "Compare IV / HV" }, { to: "/compare/straddle", label: "Compare straddles" }] },
    ] },
    { label: "Stocks", sections: [{ items: byCat("Stocks") }] },
    { label: "Dividends", sections: [{ items: byCat("Dividends") }] },
    { label: "Backtest", sections: [{ items: [
      { to: "/backtest/stock", label: "Stock rule backtest" },
      { to: "/backtest/options", label: "Options strategy backtest" },
      { to: "/backtest/straddle-wing", label: "Straddle / wing (iron fly) backtest" },
    ] }] },
    { label: "My", sections: [{ items: [
      { to: "/watchlists", label: "Watchlists" }, { to: "/alerts", label: "Alerts & notifications" },
      { to: "/status", label: "Data status" }, { to: "/settings", label: "Settings" }, { to: "/about", label: "About & data limits" },
    ] }] },
  ].map((m) => ({ ...m, sections: m.sections.filter((s) => s.items.length) }));
}

function MenuPanel({ menu, onPick }: { menu: Menu; onPick: () => void }) {
  const count = menu.sections.reduce((n, s) => n + s.items.length, 0);
  return (
    <div className={`menu-panel ${count > 12 ? "wide" : ""}`}>
      {menu.sections.map((s, i) => (
        <div className="menu-section" key={i}>
          {s.title && <div className="menu-section-title">{s.title}</div>}
          <ul>{s.items.map((it) => <li key={it.to}><Link to={it.to} title={it.title} onClick={onPick}>{it.label}</Link></li>)}</ul>
        </div>
      ))}
      {!count && <div className="muted small pad">No reports available.</div>}
    </div>
  );
}

export function Header() {
  const defs = useApi(api.reportDefs, []);
  const menus = useMemo(() => buildMenus(defs.data ?? []), [defs.data]);
  const { theme, toggle } = useTheme();
  const [open, setOpen] = useState<string | null>(null);
  const [drawer, setDrawer] = useState(false);
  const navRef = useRef<HTMLElement>(null);
  const loc = useLocation();
  const closeAll = useCallback(() => { setOpen(null); setDrawer(false); }, []);
  useClickOutside(navRef, () => setOpen(null), open !== null);
  useEffect(() => closeAll(), [loc.pathname, loc.search, closeAll]);

  return (
    <header className="app-header">
      <div className="header-row">
        <button className="icon-btn hamburger" aria-label="Menu" aria-expanded={drawer} onClick={() => setDrawer((d) => !d)}>
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
        </button>
        <Link to="/" className="brand" aria-label="OptionLens home">
          <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true"><circle cx="14" cy="14" r="8" fill="none" stroke="var(--accent)" strokeWidth="3" /><path d="M20 20l6 6" stroke="var(--accent)" strokeWidth="3" strokeLinecap="round" /><path d="M10 16l2.5-3 2 2 3-4" fill="none" stroke="var(--series-3)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
          <span>Option<b>Lens</b></span>
        </Link>
        <nav ref={navRef} className={`main-nav ${drawer ? "open" : ""}`} aria-label="Main">
          {menus.map((m) => (
            <div key={m.label} className={`nav-item ${open === m.label ? "open" : ""}`}>
              <button className="nav-btn" aria-expanded={open === m.label} onClick={() => setOpen(open === m.label ? null : m.label)}>
                {m.label}<span className="caret" aria-hidden="true">▾</span>
              </button>
              {open === m.label && <MenuPanel menu={m} onPick={closeAll} />}
            </div>
          ))}
        </nav>
        <div className="header-tools">
          <SymbolSearch onNavigate={closeAll} />
          <BridgeIndicator />
          <NotificationBell />
          <button className="icon-btn" onClick={toggle} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`} title="Toggle theme">
            {theme === "dark"
              ? <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><circle cx="12" cy="12" r="4.5" fill="currentColor" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
              : <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" fill="currentColor" /></svg>}
          </button>
        </div>
      </div>
    </header>
  );
}

export function Footer() {
  return (
    <footer className="app-footer">
      <span>OptionLens · data from IBKR (real-time or <span className="badge warn">delayed</span> per your subscriptions) · model-priced rows are labelled <span className="badge warn">model</span></span>
      <span><NavLink to="/status">Data status</NavLink> · <NavLink to="/about">About & limitations</NavLink></span>
    </footer>
  );
}
