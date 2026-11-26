// Global header: brand, nav menus (report entries come from GET /api/reports), search, bell, theme.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api } from "../api/client";
import type { ReportDef } from "../api/types";
import { useApi } from "../api/useApi";
import { useTheme } from "../state/theme";
import { LiveIndicator } from "../live";
import { SourcesIndicator } from "./Desktop";
import { NotificationBell } from "./NotificationBell";
import { SymbolSearch } from "./SymbolSearch";
import { useClickOutside } from "./ui";

export const INDEX_SYMBOLS = "SPX,NDX,RUT,XSP";

interface MenuItem { to: string; label: string; title?: string }
interface MenuSection { title?: string; items: MenuItem[] }
interface Menu { label: string; sections: MenuSection[] }

const reportItem = (d: ReportDef): MenuItem => ({ to: `/reports/${d.id}`, label: d.title, title: d.description });

/** Report links that also appear in the Market menu (one name per report: always its engine title). */
const MARKET_SNAPSHOT = ["market-movers", "performance-by-sector", "options-broad-view", "earnings-upcoming-summary"];

/** Where a menu link came from, so a report page's breadcrumb names the menu the user used (Market vs Stocks). */
export interface NavFrom { from?: string }

export function buildMenus(defs: ReportDef[]): Menu[] {
  const byCat = (c: ReportDef["category"]) => defs.filter((d) => d.category === c).map(reportItem);
  const byId = new Map(defs.map((d) => [d.id, d]));
  return [
    { label: "Market", sections: [
      { items: [{ to: "/", label: "Dashboard" }, { to: "/reports", label: "All reports" }] },
      { title: "Snapshot", items: MARKET_SNAPSHOT.flatMap((id) => { const d = byId.get(id); return d ? [reportItem(d)] : []; }) },
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
      { to: "/watchlists", label: "Watchlists" }, { to: "/portfolio", label: "Portfolio" }, { to: "/alerts", label: "Alerts & notifications" },
      { to: "/status", label: "Data status" }, { to: "/settings", label: "Settings" }, { to: "/about", label: "About & data limits" },
    ] }] },
  ].map((m) => ({ ...m, sections: m.sections.filter((s) => s.items.length) }));
}

const WIDE_ITEMS = 12;
const isWide = (m: Menu) => m.sections.reduce((n, s) => n + s.items.length, 0) > WIDE_ITEMS;

/** Where a menu opens: under its button, kept inside the window (the nav scrolls sideways, so the panel is fixed). */
export function menuPosition(btn: { left: number; bottom: number }, panelWidth: number, windowWidth: number): { left: number; top: number } {
  return { left: Math.max(8, Math.min(btn.left, windowWidth - panelWidth - 8)), top: btn.bottom + 6 };
}

function MenuPanel({ menu, onPick, at }: { menu: Menu; onPick: () => void; at: { left: number; top: number } }) {
  const count = menu.sections.reduce((n, s) => n + s.items.length, 0);
  return (
    <div className={`menu-panel ${isWide(menu) ? "wide" : ""}`} style={{ "--menu-left": `${at.left}px`, "--menu-top": `${at.top}px` } as CSSProperties}>
      {menu.sections.map((s, i) => (
        <div className="menu-section" key={i}>
          {s.title && <div className="menu-section-title">{s.title}</div>}
          <ul>{s.items.map((it) => <li key={it.to}><Link to={it.to} state={{ from: menu.label } satisfies NavFrom} title={it.title} onClick={onPick}>{it.label}</Link></li>)}</ul>
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
  const [at, setAt] = useState({ left: 0, top: 0 });
  const [drawer, setDrawer] = useState(false);
  const navRef = useRef<HTMLElement>(null);
  const loc = useLocation();
  const closeAll = useCallback(() => { setOpen(null); setDrawer(false); }, []);
  useClickOutside(navRef, () => setOpen(null), open !== null);
  useEffect(() => closeAll(), [loc.pathname, loc.search, closeAll]);
  // The status pills give way before the menus do: laid out with their text first, and when the menus then do not
  // fit, collapsed to the sources' marks (both passes happen before paint). Re-tried when the window or the pills change.
  const rowRef = useRef<HTMLDivElement>(null);
  const toolsRef = useRef<HTMLDivElement>(null);
  const [compact, setCompact] = useState(false);
  const [measure, setMeasure] = useState(0);
  useLayoutEffect(() => {
    const nav = navRef.current;
    if (nav && !compact && nav.scrollWidth > nav.clientWidth + 1) setCompact(true);
  }, [compact, measure, menus]);
  useEffect(() => {
    const again = () => { setCompact(false); setMeasure((n) => n + 1); };
    const ro = new ResizeObserver(again);
    if (rowRef.current) ro.observe(rowRef.current);
    const mo = new MutationObserver(again);
    if (toolsRef.current) mo.observe(toolsRef.current, { childList: true, characterData: true, subtree: true });
    return () => { ro.disconnect(); mo.disconnect(); };
  }, []);
  const toggleMenu = (m: Menu, btn: HTMLElement) => {
    if (open === m.label) { setOpen(null); return; }
    setAt(menuPosition(btn.getBoundingClientRect(), isWide(m) ? 520 : 240, window.innerWidth));
    setOpen(m.label);
  };
  useEffect(() => {   // an open panel is fixed to where its button was: scrolling the nav or resizing closes it
    if (open === null || drawer) return;
    const close = () => setOpen(null);
    const nav = navRef.current;
    nav?.addEventListener("scroll", close);
    window.addEventListener("resize", close);
    return () => { nav?.removeEventListener("scroll", close); window.removeEventListener("resize", close); };
  }, [open, drawer]);

  return (
    <header className="app-header">
      <div className="header-row" ref={rowRef}>
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
              <button className="nav-btn" aria-expanded={open === m.label} onClick={(e) => toggleMenu(m, e.currentTarget)}>
                {m.label}<span className="caret" aria-hidden="true">▾</span>
              </button>
              {open === m.label && <MenuPanel menu={m} onPick={closeAll} at={at} />}
            </div>
          ))}
        </nav>
        <div className={`header-tools${compact ? " compact" : ""}`} ref={toolsRef}>
          <SymbolSearch onNavigate={closeAll} />
          <LiveIndicator />
          <SourcesIndicator />
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
      <span>OptionLens · option data from IBKR and/or Questrade (real-time or <span className="badge info">delayed</span> per your subscriptions) · model-priced rows are labelled <span className="badge warn">model</span></span>
      <span><NavLink to="/status">Data status</NavLink> · <NavLink to="/about">About & limitations</NavLink></span>
    </footer>
  );
}
