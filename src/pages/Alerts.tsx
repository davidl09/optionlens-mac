// /alerts — alert CRUD for every AlertKind + notification feed.
import { FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertInput, api } from "../api/client";
import type { Alert, AlertKind, Watchlist } from "../api/types";
import { useApi } from "../api/useApi";
import { Async, Card, Empty, ErrorBox, Segmented } from "../components/ui";
import { fmt, fmtDateTime } from "../lib/format";

/** unit: how params.value is typed by the user; scale converts to the stored fraction. */
export const ALERT_KINDS: Record<AlertKind, { label: string; unit: string | null; scale: number }> = {
  price_above: { label: "Price above", unit: "$", scale: 1 },
  price_below: { label: "Price below", unit: "$", scale: 1 },
  pct_change_abs: { label: "|% change| at least", unit: "%", scale: 100 },
  iv30_above: { label: "IV30 above", unit: "vol pts", scale: 100 },
  iv30_below: { label: "IV30 below", unit: "vol pts", scale: 100 },
  iv_rank_above: { label: "IV rank above", unit: "%", scale: 100 },
  iv_rank_below: { label: "IV rank below", unit: "%", scale: 100 },
  rel_option_volume_above: { label: "Relative option volume above", unit: "× avg", scale: 1 },
  unusual_volume: { label: "Appears in unusual option volume", unit: null, scale: 1 },
  earnings_within_days: { label: "Earnings within", unit: "days", scale: 1 },
  ex_div_within_days: { label: "Ex-dividend within", unit: "days", scale: 1 },
  halted: { label: "Trading halted", unit: null, scale: 1 },
  resumed: { label: "Trading resumed after a halt", unit: null, scale: 1 },
};

function describe(a: Alert, lists: Watchlist[]): string {
  const k = ALERT_KINDS[a.kind];
  const scope = a.symbol ?? (a.params.watchlist_id !== undefined ? `watchlist “${lists.find((w) => w.id === a.params.watchlist_id)?.name ?? a.params.watchlist_id}”` : "—");
  const v = a.params.value !== undefined && k.unit ? ` ${k.unit === "$" ? "$" : ""}${+(a.params.value * k.scale).toFixed(4)}${k.unit !== "$" ? ` ${k.unit}` : ""}` : "";
  return `${scope}: ${k.label}${v}`;
}

function AlertForm({ lists, initialSymbol, editing, onDone }: { lists: Watchlist[]; initialSymbol: string; editing: Alert | null; onDone: () => void }) {
  const [kind, setKind] = useState<AlertKind>(editing?.kind ?? "price_above");
  const [scope, setScope] = useState<"symbol" | "watchlist">(editing && editing.symbol === null ? "watchlist" : "symbol");
  const [symbol, setSymbol] = useState(editing?.symbol ?? initialSymbol);
  const [wl, setWl] = useState(String(editing?.params.watchlist_id ?? lists[0]?.id ?? ""));
  const meta = ALERT_KINDS[kind];
  const [value, setValue] = useState(editing?.params.value !== undefined ? String(+(editing.params.value * ALERT_KINDS[editing.kind].scale).toFixed(4)) : "");
  const [cooldown, setCooldown] = useState(String(editing?.cooldown_min ?? 240));
  const [enabled, setEnabled] = useState(editing?.enabled ?? true);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (scope === "symbol" && !symbol.trim()) { setErr("Enter a symbol."); return; }
    if (scope === "watchlist" && !wl) { setErr("Create a watchlist first."); return; }
    if (meta.unit && (value.trim() === "" || !Number.isFinite(Number(value)))) { setErr("Enter a threshold value."); return; }
    const body: AlertInput = {
      kind, enabled, cooldown_min: cooldown.trim() !== "" && Number.isFinite(Number(cooldown)) ? Number(cooldown) : 240,
      symbol: scope === "symbol" ? symbol.trim().toUpperCase() : null,
      params: { ...(meta.unit ? { value: Number(value) / meta.scale } : {}), ...(scope === "watchlist" ? { watchlist_id: Number(wl) } : {}) },
    };
    try {
      if (editing) await api.updateAlert(editing.id, body); else await api.createAlert(body);
      onDone();
    } catch (ex) { setErr(ex instanceof Error ? ex.message : String(ex)); }
  };

  return (
    <form className="param-form" onSubmit={submit}>
      <div className="param"><label htmlFor="al-kind">Condition</label>
        <select id="al-kind" value={kind} onChange={(e) => setKind(e.target.value as AlertKind)}>
          {(Object.keys(ALERT_KINDS) as AlertKind[]).map((k) => <option key={k} value={k}>{ALERT_KINDS[k].label}</option>)}
        </select>
      </div>
      {meta.unit && <div className="param"><label htmlFor="al-value">Value ({meta.unit})</label><input id="al-value" type="number" step="any" className="w-num" value={value} onChange={(e) => setValue(e.target.value)} /></div>}
      <div className="param"><label>Scope</label>
        <Segmented label="Scope" value={scope} onChange={setScope} options={[{ value: "symbol", label: "Symbol" }, { value: "watchlist", label: "Watchlist" }]} />
      </div>
      {scope === "symbol"
        ? <div className="param"><label htmlFor="al-sym">Symbol</label><input id="al-sym" className="w-sym" value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} /></div>
        : <div className="param"><label htmlFor="al-wl">Watchlist</label><select id="al-wl" value={wl} onChange={(e) => setWl(e.target.value)}>{lists.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}</select></div>}
      <div className="param"><label htmlFor="al-cd">Cooldown (min)</label><input id="al-cd" type="number" className="w-num" value={cooldown} onChange={(e) => setCooldown(e.target.value)} /></div>
      <div className="param param-bool"><label htmlFor="al-en">Enabled</label><input id="al-en" type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /></div>
      <div className="param param-submit">
        <button className="btn btn-primary" type="submit">{editing ? "Save alert" : "Create alert"}</button>
        {editing && <button className="btn btn-ghost" type="button" onClick={onDone}>Cancel</button>}
      </div>
      {err && <div className="neg small full-row">{err}</div>}
    </form>
  );
}

function Feed() {
  const [unread, setUnread] = useState(false);
  const feed = useApi(() => api.notifications(unread), [unread]);
  const mark = async (ids?: number[]) => { try { await api.markNotificationsRead(ids); feed.refetch(); } catch { /* shown on next load */ } };
  return (
    <Card title="Notifications" actions={
      <div className="toolbar">
        <Segmented label="Show" value={unread ? "unread" : "all"} onChange={(v) => setUnread(v === "unread")} options={[{ value: "all", label: "All" }, { value: "unread", label: "Unread" }]} />
        <button className="btn btn-sm" onClick={() => void mark()}>Mark all read</button>
      </div>
    }>
      <Async state={feed}>{(ns) => ns.length ? (
        <ul className="feed">
          {ns.map((n) => (
            <li key={n.id} className={n.read ? "read" : "unread"}>
              <div className="feed-head">
                <b>{n.link ? <Link to={n.link}>{n.title}</Link> : n.title}</b>
                <span className="muted small">{fmtDateTime(n.ts)}</span>
              </div>
              <div className="feed-body">{n.body}</div>
              {!n.read && <button className="link-btn small" onClick={() => void mark([n.id])}>mark read</button>}
            </li>
          ))}
        </ul>
      ) : <Empty>No notifications.</Empty>}</Async>
    </Card>
  );
}

export default function Alerts() {
  const [sp] = useSearchParams();
  const alerts = useApi(api.alerts, []);
  const lists = useApi(api.watchlists, []);
  const [editing, setEditing] = useState<Alert | null>(null);
  const [formKey, setFormKey] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const done = () => { setEditing(null); setFormKey((k) => k + 1); alerts.refetch(); };
  const act = async (p: Promise<unknown>) => { setErr(null); try { await p; alerts.refetch(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } };
  const wls = lists.data ?? [];

  return (
    <div className="stack">
      <div className="page-head"><div><h1 className="page-title">Alerts</h1><p className="page-desc">Evaluated after every sweep. Delivered in-app, plus ntfy / e-mail when configured on the server.</p></div></div>
      <Card title={editing ? `Edit alert #${editing.id}` : "New alert"}>
        <AlertForm key={editing ? `e${editing.id}` : `n${formKey}`} lists={wls} initialSymbol={sp.get("symbol") ?? ""} editing={editing} onDone={done} />
      </Card>
      <div className="grid-2">
        <Card title="Your alerts">
          {err && <ErrorBox error={err} />}
          <Async state={alerts}>{(as) => as.length ? (
            <div className="dt-scroll">
              <table className="dt">
                <thead><tr><th>Alert</th><th>On</th><th>Cooldown</th><th>Last fired</th><th /></tr></thead>
                <tbody>
                  {as.map((a) => (
                    <tr key={a.id} className={a.enabled ? "" : "muted"}>
                      <td>{describe(a, wls)}</td>
                      <td><input type="checkbox" checked={a.enabled} aria-label="Enabled" onChange={() => void act(api.updateAlert(a.id, { enabled: !a.enabled }))} /></td>
                      <td className="num">{fmt(a.cooldown_min, "int")}m</td>
                      <td>{a.last_fired_at ? fmtDateTime(a.last_fired_at) : <span className="muted">never</span>}</td>
                      <td className="row-actions">
                        <button className="btn btn-sm btn-ghost" onClick={() => setEditing(a)}>Edit</button>
                        <button className="btn btn-sm btn-ghost" onClick={() => { if (window.confirm(`Delete alert “${describe(a, wls)}”?`)) void act(api.deleteAlert(a.id)); }}>Delete</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <Empty>No alerts yet. Create one with the form above.</Empty>}</Async>
        </Card>
        <Feed />
      </div>
    </div>
  );
}
