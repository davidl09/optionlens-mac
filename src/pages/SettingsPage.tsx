// /settings — desktop app settings: the engine (status, restart, data import), the chain sources side by side (IBKR and
// Questrade, the same status fields), the IBKR bridge (live work, connection settings, cancel work), launch at login,
// updates and AI agents (MCP).
import { FormEvent, useEffect, useState } from "react";
import { JOB_KINDS, type AppSettings, type BridgeSettings, type BridgeStatus, type EngineStatus, type LoginItemStatus } from "../../electron/shared/api";
import { reloadAll } from "../api/client";
import { bridgeSummary, bridgeTone } from "../components/Desktop";
import { StatGrid } from "../components/StatGrid";
import { McpCard } from "../components/McpCard";
import { QuestradeCard } from "../components/QuestradeCard";
import { SourceStateBadge, SourceSummary } from "../components/SourceSummary";
import { ibkrView } from "../lib/sources";
import { Card, ErrorBox, InfoBox, PathText, Progress, Spinner } from "../components/ui";
import { UpdatesCard } from "../components/UpdatesCard";
import { desktop } from "../desktop";
import { farmsSummary, optionsSubscriptionText, populateText } from "../lib/status";
import { useBridgeStatus, useEngineStatus, useUpdateStatus } from "../state/desktop";

export const DATA_SOURCES_TEXT =
  "IBKR and Questrade are equal sources: when both deliver a chain, the better one is shown — a closing snapshot over an intraday one, real-time over delayed, more quoted contracts, then the fresher fetch. With both connected, Questrade sweeps the universe and IBKR adds what only it provides (IV/HV history, index history, option-bar catch-up) and the symbols Questrade could not fetch; if Questrade is unavailable, IBKR takes over its chains.";

const PORTS: { value: string; label: string }[] = [
  { value: "auto", label: "Automatic (4001 → 7496 → 4002 → 7497)" },
  { value: "4001", label: "4001 — IB Gateway, live" }, { value: "4002", label: "4002 — IB Gateway, paper" },
  { value: "7496", label: "7496 — TWS, live" }, { value: "7497", label: "7497 — TWS, paper" },
  { value: "custom", label: "Other…" },
];

function EngineCard({ engine }: { engine: EngineStatus }) {
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  useEffect(() => desktop?.onImportProgress((p) => setProgress(p.total > 0 ? p.copied / p.total : null)), []);
  const importData = async () => {
    if (!desktop) return;
    if (!window.confirm("Import an existing optionlens.duckdb? The engine stops, the file is copied into the app's data folder (the current database is kept as a backup) and the engine restarts.")) return;
    setBusy(true);
    setProgress(null);
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
        { label: "Version", value: engine.version, fmt: "text", hint: "The engine has not reported its version yet." },
        { label: "Restarts", value: engine.restarts, fmt: "int" },
        { label: "Data folder", node: <PathText path={engine.dataDir} /> },
      ]} />
      {engine.lastError && <p className="muted small">Last error: {engine.lastError}</p>}
      <div className="toolbar">
        <button className="btn" onClick={() => void desktop?.restartEngine()}>Restart engine</button>
        <button className="btn" onClick={() => void importData()} disabled={busy}>{busy ? `Importing…${progress !== null ? ` ${Math.round(progress * 100)}%` : ""}` : "Import data…"}</button>
      </div>
      {msg && (msg.ok ? <InfoBox>{msg.text}</InfoBox> : <ErrorBox error={msg.text} />)}
    </Card>
  );
}

/** IBKR as a chain source: the same fields as Questrade's card (the bridge's own work is in the card below). */
function IbkrSourceCard({ b }: { b: BridgeStatus }) {
  const v = ibkrView(b);
  return (
    <Card title="IBKR" subtitle={<SourceStateBadge v={v} />}>
      <SourceSummary v={v} />
      <p className="muted small">Via the IBKR bridge and your IB Gateway / TWS on this computer (connection settings below).</p>
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
        { label: "Endpoint", value: b.endpoint ? `${b.endpoint} · port ${b.port}` : null, fmt: "text", hint: "Not connected to IB Gateway / TWS." },
        { label: "Account", value: b.accountType, fmt: "text", hint: "Known once connected." },
        { label: "Last push", value: b.lastPushAt, fmt: "datetime", hint: "Nothing pushed yet." },
        { label: "Working on", node: <span>{b.current ? b.current.label : "idle"}</span> },
        { label: "Queued", node: <span>{queued || "nothing"}</span> },
        ...(b.tws === "connected" ? [
          { label: "IBKR servers", node: <span title="As IB Gateway / TWS reports IBKR's server farms (messages 2103–2158)">{farmsSummary({ marketData: b.farms.marketData.state, historical: b.farms.historical.state, secDef: b.farms.secDef.state })}</span> },
          { label: "US options", node: <span>{optionsSubscriptionText(b.optionsSubscription)}</span> },
        ] : []),
        ...(b.populate ? [{ label: "Last session", node: <Progress value={b.populate.total ? b.populate.done / b.populate.total : 0} label={populateText(b) ?? ""} title={`The last session (${b.populate.sessionDate}) had no data yet: the bridge collects its closing values from IBKR (frozen data) — underlying stats for every symbol, then chains`} /> }] : []),
        { label: "Backfill", node: b.backfill.total
          ? <Progress value={done / b.backfill.total} label={`${done.toLocaleString()} / ${b.backfill.total.toLocaleString()}${b.backfill.failed ? ` (${b.backfill.failed.toLocaleString()} failed)` : ""}`} title="IV/HV history and option-bar catch-up: symbols up to date / all symbols the app keeps history for" />
          : <span className="muted">nothing to catch up</span> },
      ]} />
      {b.paused && <div className="warn-box">{b.paused}</div>}
      {b.notes.map((n) => <div className={/delayed/i.test(n) ? "info-box" : "warn-box"} key={n}>{n}</div>)}
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
    <Card title="Bridge connection" subtitle="IB Gateway or TWS on this computer">
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
  const update = useUpdateStatus();
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [loginItem, setLoginItem] = useState<LoginItemStatus | null>(null);
  useEffect(() => { void desktop?.settings().then(setSettings); void desktop?.loginItemStatus().then(setLoginItem); }, []);

  if (!desktop) {
    return (
      <div className="stack">
        <div className="page-head"><h1 className="page-title">Settings</h1></div>
        <InfoBox>Settings (engine, IBKR bridge, launch at login, updates) are available in the OptionLens desktop app.</InfoBox>
      </div>
    );
  }
  const save = async (next: AppSettings): Promise<string | null> => {
    const r = await desktop!.saveSettings(next);
    if (!r.ok) return r.error;
    setSettings(next);
    setLoginItem(await desktop!.loginItemStatus());
    return null;
  };
  return (
    <div className="stack">
      <div className="page-head"><h1 className="page-title">Settings</h1></div>
      {engine ? <EngineCard engine={engine} /> : <Spinner />}
      <h2 className="section-title">Data sources <small className="muted">{DATA_SOURCES_TEXT}</small></h2>
      <div className="grid-2">
        {bridge ? <IbkrSourceCard b={bridge} /> : <Spinner />}
        <QuestradeCard />
      </div>
      <h2 className="section-title">IBKR bridge</h2>
      <div className="grid-2">
        {bridge ? <BridgeStatusCard b={bridge} /> : <Spinner />}
        {settings && <BridgeForm settings={settings} onSave={save} />}
      </div>
      {settings && (
        <Card title="General">
          <label className="check-inline">
            <input type="checkbox" checked={settings.launchAtLogin} onChange={(e) => void save({ ...settings, launchAtLogin: e.target.checked })} />
            Launch OptionLens at login (it keeps collecting in the tray when the window is closed)
          </label>
          {loginItem === "requires-approval" && (
            <InfoBox>macOS needs your approval: open <b>System Settings → General → Login Items</b> and turn on OptionLens.</InfoBox>
          )}
          <p className="muted small">Notifications from your alerts appear as system notifications; clicking one opens it here.</p>
        </Card>
      )}
      {settings && update && <UpdatesCard u={update} settings={settings} onSave={save} />}
      <McpCard />
    </div>
  );
}
