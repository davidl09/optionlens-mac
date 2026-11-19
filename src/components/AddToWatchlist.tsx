// "Watch" button: toggle a symbol's membership in any watchlist, or create a new list with it.
import { FormEvent, useCallback, useRef, useState } from "react";
import { api } from "../api/client";
import { useApi } from "../api/useApi";
import { useClickOutside } from "./ui";

export function AddToWatchlist({ symbol }: { symbol: string }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useClickOutside(ref, close, open);
  const lists = useApi(api.watchlists, []);
  const member = (lists.data ?? []).filter((w) => w.symbols.includes(symbol)).length;

  const run = async (p: Promise<unknown>) => {
    setErr(null);
    try { await p; lists.refetch(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  const create = (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    void run(api.createWatchlist({ name: name.trim(), symbols: [symbol] }));
    setName("");
  };

  return (
    <div className="pop-anchor" ref={ref}>
      <button className={`btn btn-sm ${member ? "btn-on" : ""}`} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {member ? "★ Watching" : "☆ Watch"}
      </button>
      {open && (
        <div className="popover right watch-pop">
          <div className="filter-title">Watchlists for {symbol}</div>
          {(lists.data ?? []).map((w) => {
            const on = w.symbols.includes(symbol);
            return (
              <label key={w.id} className="check-row">
                <input type="checkbox" checked={on} onChange={() => void run(on ? api.removeFromWatchlist(w.id, symbol) : api.addToWatchlist(w.id, symbol))} />
                {w.name} <small className="muted">({w.symbols.length})</small>
              </label>
            );
          })}
          {lists.data && !lists.data.length && <div className="muted small">No watchlists yet.</div>}
          <form onSubmit={create} className="inline-form">
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="New list name" aria-label="New watchlist name" />
            <button className="btn btn-sm" type="submit">Create</button>
          </form>
          {err && <div className="neg small">{err}</div>}
        </div>
      )}
    </div>
  );
}
