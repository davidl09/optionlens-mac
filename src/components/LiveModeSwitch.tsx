// Settings → Data sources → Questrade: "Stream live quotes (tick-by-tick)" on = Level 1 streaming, off = polling
// (INTEGRATION.md §9, PUT /api/live/mode; in effect at once). Questrade's warning sits under the switch, then the live
// status (polled while the card is open).
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { LiveMode, LiveStatusReport } from "../api/types";
import { liveModeStatusText, STREAM_WARNING } from "../lib/liveMode";
import { ErrorBox } from "./ui";

const STATUS_POLL_MS = 3_000;

export function LiveModeSwitch() {
  const [mode, setMode] = useState<LiveMode | null>(null);
  const [report, setReport] = useState<LiveStatusReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.liveMode().then((m) => { if (alive) setMode(m.mode); }, (e: unknown) => { if (alive) setError(e instanceof Error ? e.message : String(e)); });
    const poll = () => api.liveStatus().then((r) => { if (alive) setReport(r); }, () => { if (alive) setReport(null); });
    void poll();
    const t = setInterval(() => void poll(), STATUS_POLL_MS);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const toggle = async () => {
    if (mode === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      setMode((await api.setLiveMode(mode === "stream" ? "poll" : "stream")).mode);
      setReport(await api.liveStatus().catch(() => null));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const on = mode === "stream";
  return (
    <div className="live-mode">
      <div className="switch-row">
        <button type="button" role="switch" className="switch" aria-checked={on} aria-labelledby="live-mode-label"
          disabled={mode === null} aria-busy={busy} onClick={() => void toggle()}>
          <span className="switch-knob" aria-hidden="true" />
        </button>
        <span id="live-mode-label" className="switch-label">Stream live quotes (tick-by-tick)</span>
        <span className="muted small">{mode === null ? "" : on ? "On: Level 1 streaming" : "Off: polling every few seconds"}</span>
      </div>
      <p className="small warn-text live-mode-warning">{STREAM_WARNING}</p>
      {mode !== null && <p className="small muted" role="status" data-testid="live-mode-status">{liveModeStatusText(mode, report)}</p>}
      {error && <ErrorBox error={error} />}
    </div>
  );
}
