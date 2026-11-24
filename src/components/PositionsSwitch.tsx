// Settings → Data sources → Questrade / IBKR: "Show my positions" (INTEGRATION.md §11; on by default). Off hides that
// broker's positions on the Portfolio page and stops fetching them (no Questrade account call, the IBKR bridge ends its
// read-only positions feed).
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Broker, PortfolioSettings } from "../api/types";
import { ErrorBox } from "./ui";

export function PositionsSwitch({ broker }: { broker: Broker }) {
  const [settings, setSettings] = useState<PortfolioSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api.portfolioSettings().then((s) => { if (alive) setSettings(s); }, (e: unknown) => { if (alive) setError(e instanceof Error ? e.message : String(e)); });
    return () => { alive = false; };
  }, []);
  const on = settings?.[broker] ?? false;
  const toggle = async () => {
    if (!settings || busy) return;
    setBusy(true);
    setError(null);
    try { setSettings(await api.setPortfolioSettings({ [broker]: !on })); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const id = `positions-${broker}-label`;
  return (
    <div className="positions-switch">
      <div className="switch-row">
        <button type="button" role="switch" className="switch" aria-checked={on} aria-labelledby={id} disabled={!settings} aria-busy={busy} onClick={() => void toggle()}>
          <span className="switch-knob" aria-hidden="true" />
        </button>
        <span id={id} className="switch-label">Show my positions</span>
        <span className="muted small">{settings === null ? "" : on ? "On: My → Portfolio (read-only)" : "Off: hidden, not fetched"}</span>
      </div>
      {error && <ErrorBox error={error} />}
    </div>
  );
}
