// /watchlists — CRUD + quick table per list.
import { FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { Watchlist } from "../api/types";
import { useApi } from "../api/useApi";
import { DataTable } from "../components/DataTable";
import { Async, Card, Empty, ErrorBox } from "../components/ui";
import { parseSymbols } from "../lib/params";

function useAction(onDone: () => void) {
  const [err, setErr] = useState<string | null>(null);
  const run = async (p: Promise<unknown>) => {
    setErr(null);
    try { await p; onDone(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return { err, run };
}

function ListDetail({ w, onChanged, onDeleted }: { w: Watchlist; onChanged: () => void; onDeleted: () => void }) {
  const table = useApi(() => api.watchlistTable(w.id), [w.id, w.symbols.join(",")]);
  const [add, setAdd] = useState("");
  const [name, setName] = useState(w.name);
  const { err, run } = useAction(onChanged);
  const addSymbols = async (e: FormEvent) => {
    e.preventDefault();
    const syms = parseSymbols(add).filter((s) => !w.symbols.includes(s));
    if (!syms.length) return;
    await run(api.updateWatchlist(w.id, { symbols: [...w.symbols, ...syms] }));
    setAdd("");
  };
  return (
    <div className="stack">
      <Card title={w.name} subtitle={`${w.symbols.length} symbols`} actions={
        <div className="toolbar">
          <form className="inline-form" onSubmit={(e) => { e.preventDefault(); if (name.trim() && name !== w.name) void run(api.updateWatchlist(w.id, { name: name.trim() })); }}>
            <input value={name} onChange={(e) => setName(e.target.value)} aria-label="Watchlist name" />
            <button className="btn btn-sm" type="submit">Rename</button>
          </form>
          <button className="btn btn-sm btn-danger" onClick={() => { if (window.confirm(`Delete watchlist “${w.name}”?`)) void api.deleteWatchlist(w.id).then(onDeleted); }}>Delete</button>
        </div>
      }>
        <form className="inline-form" onSubmit={addSymbols}>
          <input value={add} onChange={(e) => setAdd(e.target.value.toUpperCase())} placeholder="Add symbols: AAPL, MSFT" aria-label="Add symbols" className="w-wide" />
          <button className="btn btn-sm btn-primary" type="submit">Add</button>
        </form>
        <div className="chips top-gap">
          {w.symbols.map((s) => (
            <span key={s} className="chip"><Link to={`/s/${s}`}>{s}</Link><button className="link-btn" aria-label={`Remove ${s}`} onClick={() => void run(api.removeFromWatchlist(w.id, s))}>×</button></span>
          ))}
          {!w.symbols.length && <span className="muted small">Empty list.</span>}
        </div>
        {err && <ErrorBox error={err} />}
      </Card>
      <Card className="flush">
        <Async state={table}>{(r) => <DataTable columns={r.columns} rows={r.rows} csvName={`watchlist-${w.name}`} />}</Async>
      </Card>
    </div>
  );
}

export default function Watchlists() {
  const lists = useApi(api.watchlists, []);
  const [sp, setSp] = useSearchParams();
  const [newName, setNewName] = useState("");
  const { err, run } = useAction(lists.refetch);
  const selId = Number(sp.get("id") ?? lists.data?.[0]?.id ?? 0);
  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    try {
      const w = await api.createWatchlist({ name: newName.trim(), symbols: [] });
      setNewName(""); lists.refetch(); setSp({ id: String(w.id) });
    } catch (ex) { void run(Promise.reject(ex)); }
  };
  return (
    <div className="stack">
      <div className="page-head"><h1 className="page-title">Watchlists</h1></div>
      <Async state={lists}>{(ls) => {
        const sel = ls.find((w) => w.id === selId) ?? ls[0];
        return (
          <div className="grid-side-main">
            <Card title="Lists">
              <ul className="side-list">
                {ls.map((w) => (
                  <li key={w.id}><button className={w.id === sel?.id ? "active" : ""} onClick={() => setSp({ id: String(w.id) })}>{w.name} <small className="muted">{w.symbols.length}</small></button></li>
                ))}
              </ul>
              {!ls.length && <Empty>No watchlists yet.</Empty>}
              <form className="inline-form top-gap" onSubmit={create}>
                <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="New list name" aria-label="New watchlist name" />
                <button className="btn btn-sm btn-primary" type="submit">Create</button>
              </form>
              {err && <ErrorBox error={err} />}
            </Card>
            {sel ? <ListDetail key={sel.id} w={sel} onChanged={lists.refetch} onDeleted={() => { setSp({}); lists.refetch(); }} /> : <Card><Empty>Create a watchlist to get started.</Empty></Card>}
          </div>
        );
      }}</Async>
    </div>
  );
}
