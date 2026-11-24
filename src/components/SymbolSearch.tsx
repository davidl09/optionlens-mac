// Header symbol search with autocomplete (GET /api/search) and keyboard navigation.
import { KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { SymbolRef } from "../api/types";
import { useApi, useDebounced } from "../api/useApi";
import { rankSymbols } from "../lib/search";
import { useClickOutside } from "./ui";

export function SymbolSearch({ onNavigate }: { onNavigate?: () => void }) {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const box = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const dq = useDebounced(q.trim(), 150);
  const res = useApi(dq ? () => api.search(dq) : null, [dq]);
  const items: SymbolRef[] = useMemo(() => (dq ? rankSymbols(dq, res.data ?? []) : []), [dq, res.data]);
  const list = useRef<HTMLUListElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useClickOutside(box, close, open);

  useEffect(() => setActive(-1), [dq]);
  useEffect(() => { list.current?.children[active]?.scrollIntoView({ block: "nearest" }); }, [active]);
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(t.tagName)) { e.preventDefault(); input.current?.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const go = (symbol: string) => {
    const s = symbol.trim().toUpperCase().replace(/^\^/, "");
    if (!s) return;
    setQ(""); setOpen(false);
    input.current?.blur();
    navigate(`/s/${encodeURIComponent(s)}`);
    onNavigate?.();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive((a) => Math.min(items.length - 1, a + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(-1, a - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); go(active >= 0 && items[active] ? items[active].symbol : q); }
    else if (e.key === "Escape") { setOpen(false); input.current?.blur(); }
  };

  return (
    <div className="symbol-search" ref={box}>
      <input ref={input} type="search" value={q} placeholder="Symbol or company  ( / )" aria-label="Search symbols" spellCheck={false} autoComplete="off"
        role="combobox" aria-expanded={open && items.length > 0} aria-controls="symbol-search-list"
        aria-activedescendant={active >= 0 && items[active] ? `symbol-opt-${items[active].symbol}` : undefined}
        onChange={(e) => { setQ(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onKeyDown={onKeyDown} />
      {open && items.length > 0 && (
        <ul className="search-list" id="symbol-search-list" role="listbox" ref={list}>
          {items.map((it, i) => (
            <li key={it.symbol} id={`symbol-opt-${it.symbol}`} role="option" aria-selected={i === active} className={i === active ? "active" : undefined}
              onMouseEnter={() => setActive(i)} onMouseDown={(e) => { e.preventDefault(); go(it.symbol); }}>
              <b>{it.symbol}</b><span className="search-name">{it.name}</span><span className="search-kind">{it.kind} · {it.exchange ?? "US"} · {it.currency}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
