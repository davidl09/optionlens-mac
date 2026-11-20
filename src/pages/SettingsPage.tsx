// /settings — desktop app settings: the engine (status, restart, data import), the IBKR bridge
// (live status, connection settings, cancel work) and launch at login.
import { FormEvent, useEffect, useState } from "react";
import { JOB_KINDS, type AppSettings, type BridgeSettings, type BridgeStatus, type EngineStatus } from "../../electron/shared/api";
import { reloadAll } from "../api/client";
import { bridgeSummary, bridgeTone } from "../components/Desktop";
import { StatGrid } from "../components/StatGrid";
import { Card, ErrorBox, InfoBox, Spinner } from "../components/ui";
import { desktop } from "../desktop";
import { useBridgeStatus, useEngineStatus } from "../state/desktop";

const PORTS: { value: string; label: string }[] = [
  { value: "auto", label: "Automatic (4001 → 7496 → 4002 → 7497)" },
  { value: "4001", label: "4001 — IB Gateway, live" }, { value: "4002", label: "4002 — IB Gateway, paper" },
  { value: "7496", label: "7496 — TWS, live" }, { value: "7497", label: "7497 — TWS, paper" },
  { value: "custom", label: "Other…" },
];

function EngineCard({ engine }: { engine: EngineStatus }) {
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const importData = async () => {
    if (!desktop) return;
    if (!window.confirm("Import an existing optionlens.duckdb? The engine stops, the file is copied into the app's data folder (the current database is kept as a backup) and the engine restarts.")) return;
    setBusy(true);
    const r = await desktop.importData();
    setBusy(false);
    if (r.ok) { setMsg({ ok: true, text: "Imported. The engine is restarting with the imported data." }); reloadAll(); }
    else if ("cancelled" in r) setMsg(null);
    else setMsg({ ok: false, text: r.error });
  };
  const tone = engine.state === "running" ? "pos" : engine.state === "failed" ? "neg" : "warn";
  return (
    <Card title="Engine" subtitle="The local analytics engine and its database">
      <StatGrid items={[
        { label: "State", node: <span className={`badge ${tone}`}>{engine.state}</span> },
        { label: "Version", node: <span>{engine.version ?? "—"}</span> },
        { label: "Restarts", value: engine.restarts, fmt: "int" },
        { label: "Data folder", node: <code className="wrap">{engine.dataDir}</code> },
      ]} />
      {engine.lastError && <p className="muted small">Last error: {engine.lastError}</p>}
      <div className="toolbar">
        <button className="btn" onClick={() => void desktop?.restartEngine()}>Restart engine</button>
        <button className="btn" onClick={() => void importData()} disabled={busy}>{busy ? "Importing…" : "Import data…"}</button>
      </div>
      {msg && (msg.ok ? <InfoBox>{msg.text}</InfoBox> : <ErrorBox error={msg.text} />)}
    </Card>
  );
}

function BridgeStatusCard({ b }: { b: BridgeStatus }) {
  const queued = JOB_KINDS.filter((k) => b.queued[k] > 0).map((k) => `${k} ${b.queued[k]}`).join(", ");
  const done = b.backfill.done + b.backfill.failed;
  return (
    <Card title="IBKR bridge" subtitle={<span className={`badge ${bridgeTone(b)}`}>{b.tws}</span>}>
      <p className="bridge-summary">{bridgeSummary(b)}</p>
      <StatGrid items={[
        { label: "Endpoint", node: <span>{b.endpoint ? `${b.endpoint} · port ${b.port}` : "—"}</span> },
        { label: "Account", node: <span>{b.accountType ?? "—"}</span> },
        { label: "Last push", value: b.lastPushAt, fmt: "datetime", hint: "Nothing pushed yet." },
        { label: "Working on", node: <span>{b.current ? b.current.label : "idle"}</span> },
        { label: "Queued", node: <span>{queued || "nothing"}</span> },
        { label: "Backfill", node: b.backfill.total
          ? <div className="progress" title={`${done}/${b.backfill.total}`}><div style={{ width: `${Math.round((done / b.backfill.total) * 100)}%` }} /><span>{done}/{b.backfill.total}{b.backfill.failed ? ` (${b.backfill.failed} failed)` : ""}</span></div>
          : <span>—</span> },
      ]} />
      {b.paused && <div className="warn-box">{b.paused}</div>}
      {b.notes.map((n) => <div className="warn-box" key={n}>{n}</div>)}
      {b.lastError && b.tws === "connected" && <p className="muted small">Last error: {b.lastError}</p>}
      <div className="toolbar">
        <button className="btn" onClick={() => void desktop?.cancelJobs("iv-history")}>Cancel IV backfill</button>
        <button className="btn" onClick={() => void desktop?.cancelJobs("option-bars")}>Cancel option-bar catch-up</button>
        <button className="btn btn-danger" onClick={() => void desktop?.cancelJobs()}>Cancel all bridge work</button>
      </div>
    </Card>
  );
}

function BridgeForm({ settings, onSave }: { settings: AppSettings; onSave: (s: AppSettings) => Promise<string | null> }) {
  const b = settings.bridge;
  const known = PORTS.some((p) => p.value === String(b.port));
  const [form, setForm] = useState({
    enabled: b.enabled, portMode: b.port === null ? "auto" : known ? String(b.port) : "custom", customPort: b.port === null ? "" : String(b.port),
    clientId: String(b.clientId), lines: String(b.marketDataLines),
  });
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const port = form.portMode === "auto" ? null : Number(form.portMode === "custom" ? form.customPort : form.portMode);
    const bridge: BridgeSettings = { enabled: form.enabled, port, clientId: Number(form.clientId), marketDataLines: Number(form.lines) };
    const err = await onSave({ ...settings, bridge });
    setMsg(err ? { ok: false, text: err } : { ok: true, text: "Saved. The bridge reconnects with the new settings." });
  };
  return (
    <Card title="Bridge connection" subtitle="IB Gateway or TWS on this Mac">
      <form className="settings-form" onSubmit={(e) => void submit(e)}>
        <label className="check-inline"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> Use IBKR (connect to IB Gateway / TWS)</label>
        <label>API port
          <select aria-label="API port" value={form.portMode} onChange={(e) => setForm({ ...form, portMode: e.target.value })}>
            {PORTS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
        </label>
        {form.portMode === "custom" && <label>Port<input aria-label="Custom port" inputMode="numeric" value={form.customPort} onChange={(e) => setForm({ ...form, customPort: e.target.value })} /></label>}
        <label>Client ID<input aria-label="Client ID" inputMode="numeric" value={form.clientId} onChange={(e) => setForm({ ...form, clientId: e.target.value })} /></label>
        <label>Market data lines<input aria-label="Market data lines" inputMode="numeric" value={form.lines} onChange={(e) => setForm({ ...form, lines: e.target.value })} /></label>
        <p className="muted small">Your account's simultaneous market-data line limit (100 by default at IBKR). The bridge keeps about 10% free for your own Gateway/TWS windows.</p>
        <div><button className="btn btn-primary" type="submit">Save</button></div>
        {msg && (msg.ok ? <InfoBox>{msg.text}</InfoBox> : <ErrorBox error={msg.text} />)}
      </form>
    </Card>
  );
}

export default function SettingsPage() {
  const engine = useEngineStatus();
  const bridge = useBridgeStatus();
  const [settings, setSettings] = useState<AppSettings | null>(null);
  useEffect(() => { void desktop?.settings().then(setSettings); }, []);

  if (!desktop) {
    return (
      <div className="stack">
        <div className="page-head"><h1 className="page-title">Settings</h1></div>
        <InfoBox>Settings (engine, IBKR bridge, launch at login) are available in the OptionLens desktop app.</InfoBox>
      </div>
    );
  }
  const save = async (next: AppSettings): Promise<string | null> => {
    const r = await desktop!.saveSettings(next);
    if (!r.ok) return r.error;
    setSettings(next);
    return null;
  };
  return (
    <div className="stack">
      <div className="page-head"><h1 className="page-title">Settings</h1></div>
      {engine ? <EngineCard engine={engine} /> : <Spinner />}
      <div className="grid-2">
        {bridge ? <BridgeStatusCard b={bridge} /> : <Spinner />}
        {settings && <BridgeForm settings={settings} onSave={save} />}
      </div>
      {settings && (
        <Card title="General">
          <label className="check-inline">
            <input type="checkbox" checked={settings.launchAtLogin} onChange={(e) => void save({ ...settings, launchAtLogin: e.target.checked })} />
            Launch OptionLens at login (it keeps collecting in the menu bar when the window is closed)
          </label>
          <p className="muted small">Notifications from your alerts appear as macOS notifications; clicking one opens it here.</p>
        </Card>
      )}
    </div>
  );
}
