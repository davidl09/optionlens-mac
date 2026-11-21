// Settings → Updates: the installed and latest versions, the release notes (plain text: React escapes it,
// nothing is rendered as HTML), check / download-and-install, automatic mode and the update source.
import { FormEvent, useState } from "react";
import type { AppSettings, UpdateStatus } from "../../electron/shared/api";
import { desktop } from "../desktop";
import { fmtDateTime } from "../lib/format";
import { Card, ErrorBox, InfoBox, Spinner } from "./ui";

function PhaseLine({ u }: { u: UpdateStatus }) {
  const v = u.latest?.version;
  switch (u.phase) {
    case "idle": return <p className="muted">Not checked yet.</p>;
    case "checking": return <Spinner label="Checking for updates…" />;
    case "up-to-date": return <p className="update-phase">OptionLens is up to date.</p>;
    case "available": return <p className="update-phase">Version {v} is available.</p>;
    case "downloading": {
      const pct = Math.round((u.progress ?? 0) * 100);
      return (
        <div className="update-phase">
          <p>Downloading version {v}…</p>
          <div className="progress" role="progressbar" aria-label="Update download" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <div style={{ width: `${pct}%` }} /><span>{pct}%</span>
          </div>
        </div>
      );
    }
    case "ready": return <p className="update-phase">Version {v} is downloaded and verified. OptionLens restarts to install it.</p>;
    case "installing": return <Spinner label={`Restarting to install version ${v}…`} />;
    case "error": return <ErrorBox error={u.error ?? "The update failed."} />;
  }
}

export function UpdatesCard({ u, settings, onSave }: { u: UpdateStatus; settings: AppSettings; onSave: (s: AppSettings) => Promise<string | null> }) {
  const [source, setSource] = useState(settings.updates.source);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async (updates: AppSettings["updates"], okText: string | null) => {
    const err = await onSave({ ...settings, updates });
    setMsg(err ? { ok: false, text: err } : okText ? { ok: true, text: okText } : null);
  };
  const submitSource = (e: FormEvent) => { e.preventDefault(); void save({ ...settings.updates, source: source.trim() }, "Saved. OptionLens checks this repository for updates."); };
  const install = async () => {
    setBusy(true);
    const r = await desktop!.installUpdate();
    setBusy(false);
    setMsg(r.ok ? null : { ok: false, text: r.error });
  };
  const canInstall = (u.phase === "available" || u.phase === "ready" || (u.phase === "error" && u.latest !== null && u.latest.version !== u.currentVersion)) && !busy;
  return (
    <Card title="Updates" subtitle="New versions from GitHub releases, checked at launch and every 6 hours">
      <div className="updates" data-phase={u.phase}>
        {u.notice && (
          <div className="warn-box" role="status">
            <span>{u.notice}</span>
            <button className="btn btn-sm" onClick={() => void desktop?.dismissUpdateNotice()}>Dismiss</button>
          </div>
        )}
        <dl className="update-versions">
          <dt>Installed</dt><dd>{u.currentVersion}</dd>
          <dt>Latest</dt><dd>{u.latest?.version ?? "—"}{u.checkedAt && <span className="muted small"> · checked {fmtDateTime(u.checkedAt)}</span>}</dd>
        </dl>
        <PhaseLine u={u} />
        {u.skipped && <InfoBox>This version was rolled back on this Mac, so it is not installed automatically. You can still install it here.</InfoBox>}
        {u.blocked && <div className="warn-box">{u.blocked}</div>}
        {u.latest && u.latest.version !== u.currentVersion && u.latest.notes && (
          <details className="release-notes" open={u.phase === "available"}>
            <summary>Release notes for {u.latest.version}</summary>
            <pre>{u.latest.notes}</pre>
          </details>
        )}
        <div className="toolbar">
          <button className="btn" onClick={() => void desktop?.checkForUpdates()} disabled={u.phase === "checking" || u.phase === "downloading" || u.phase === "installing"}>Check now</button>
          {canInstall && (
            <button className="btn btn-primary" onClick={() => void install()}>{u.phase === "ready" ? "Restart and install" : "Download and install"}</button>
          )}
        </div>
        <label className="check-inline">
          <input type="checkbox" checked={settings.updates.auto} onChange={(e) => void save({ ...settings.updates, auto: e.target.checked }, null)} />
          Install updates automatically (downloaded in the background, installed when the Mac is idle — never 16:15–16:50 ET on weekdays)
        </label>
        <form className="settings-form" onSubmit={submitSource}>
          <label>Update source (GitHub owner/repo)
            <input aria-label="Update source" value={source} onChange={(e) => setSource(e.target.value)} spellCheck={false} />
          </label>
          <div><button className="btn" type="submit" disabled={source.trim() === settings.updates.source}>Save source</button></div>
        </form>
        {msg && (msg.ok ? <InfoBox>{msg.text}</InfoBox> : <ErrorBox error={msg.text} />)}
      </div>
    </Card>
  );
}
