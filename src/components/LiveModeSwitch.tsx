// Settings → Data sources → Questrade: "Stream live quotes (tick-by-tick)" on = Level 1 streaming, off = polling
// (INTEGRATION.md §9, PUT /api/live/mode; in effect at once). Questrade's warning sits under the switch, then the live
// status (polled while the card is open).
import { useCallback, useEffect, useRef, useState } from "react";
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
  // Status requests overlap (the poll and the one after a toggle): only an answer to a later request than the one shown
  // is applied, so a poll sent before the switch never replaces the status that followed it.
  const asked = useRef(0);
  const shown = useRef(0);
  const alive = useRef(true);
  const refreshStatus = useCallback(async () => {
    const id = ++asked.current;
    const r = await api.liveStatus().catch(() => null);
    if (alive.current && id > shown.current) { shown.current = id; setReport(r); }
  }, []);

  useEffect(() => {
    alive.current = true;
    api.liveMode().then((m) => { if (alive.current) setMode(m.mode); }, (e: unknown) => { if (alive.current) setError(e instanceof Error ? e.message : String(e)); });
    void refreshStatus();
    const t = setInterval(() => void refreshStatus(), STATUS_POLL_MS);
    return () => { alive.current = false; clearInterval(t); };
  }, [refreshStatus]);

  const toggle = async () => {
    if (mode === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      setMode((await api.setLiveMode(mode === "stream" ? "poll" : "stream")).mode);
      await refreshStatus();
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
