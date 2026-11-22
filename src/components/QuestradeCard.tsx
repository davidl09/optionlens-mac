// Settings → Data sources → Questrade: connect with a refresh token from Questrade's API Centre, see the connection,
// call pacing and data type, disconnect (INTEGRATION.md §8). The token is sent once and never shown again.
import { FormEvent, useState } from "react";
import { api } from "../api/client";
import type { QuestradeState, QuestradeStatus } from "../api/types";
import { useApi } from "../api/useApi";
import { StatGrid } from "./StatGrid";
import { Card, ErrorBox, InfoBox, Spinner } from "./ui";

const STATE: Record<QuestradeState, { label: string; tone: "pos" | "warn" | "neg" | "neutral" }> = {
  connected: { label: "connected", tone: "pos" },
  disconnected: { label: "not connected", tone: "neutral" },
  expired: { label: "token expired", tone: "warn" },
  error: { label: "error", tone: "neg" },
};

function dataKind(s: QuestradeStatus): string | null {
  if (s.delayed === null) return null;
  return s.delayed ? "delayed (no real-time data package)" : "real-time";
}

function Help() {
  return (
    <details className="muted small">
      <summary>How to get a refresh token</summary>
      <ol>
        <li>Log in to Questrade and open <b>API Centre</b> from the menu under your name (activate the API once and accept the agreement).</li>
        <li>Click <b>Register a personal app</b>, give it a name, and save.</li>
        <li>Under the app, click <b>New manual authorization</b> → <b>Generate new token</b>, then <b>Copy token</b>.</li>
        <li>Paste it here and click Connect. OptionLens exchanges it at once; each exchange gives a new token, which the engine
          stores privately on this computer. A token that was already used or left unused too long stops working — then
          generate a new one the same way.</li>
      </ol>
      <p>Market data from the API is real-time only with a Questrade real-time data package; otherwise quotes are delayed and
        are labelled so. The engine uses at most 70% of Questrade's limits (20 calls/s, 15,000/h).</p>
    </details>
  );
}

export function QuestradeCard() {
  const loaded = useApi(() => api.questradeStatus(), []);
  const [updated, setUpdated] = useState<QuestradeStatus | null>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const s = updated ?? loaded.data;

  if (loaded.error && !s) return <Card title="Questrade"><InfoBox>Questrade is not available in this engine: {loaded.error.message}</InfoBox></Card>;
  if (!s) return <Card title="Questrade"><Spinner /></Card>;

  const run = async (fn: () => Promise<QuestradeStatus>, ok: string) => {
    setBusy(true);
    setMsg(null);
    try {
      setUpdated(await fn());
      setToken("");
      setMsg({ ok: true, text: ok });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };
  const connect = (e: FormEvent) => {
    e.preventDefault();
    if (token.trim()) void run(() => api.connectQuestrade(token.trim()), "Connected. Chains are collected on the next sweep (hourly during market hours).");
  };
  const disconnect = () => {
    if (window.confirm("Disconnect Questrade? The stored token is deleted from this computer (revoke it in Questrade's API Centre too if you no longer need it).")) {
      void run(() => api.disconnectQuestrade(), "Disconnected. The token was deleted.");
    }
  };
  const st = STATE[s.state];
  const connected = s.state !== "disconnected";
  return (
    <Card title="Questrade" subtitle={<span className={`badge ${st.tone}`}>{st.label}</span>}>
      <StatGrid items={[
        { label: "Data", value: dataKind(s), fmt: "text", hint: "Known after the first chain is fetched." },
        { label: "Calls this hour", node: <span title="Calls started in the last hour, of the engine's budget (70% of Questrade's 15,000 per hour)">{s.calls_last_hour.toLocaleString("en-US")} / {s.hour_budget.toLocaleString("en-US")}{s.remaining !== null ? ` · ${s.remaining.toLocaleString("en-US")} left at Questrade` : ""}</span> },
        { label: "Last chain", value: s.last_chain_at, fmt: "datetime", hint: "No chain from Questrade yet." },
        { label: "Last sweep", value: s.last_sweep_at, fmt: "datetime", hint: "No sweep yet.", sub: s.last_sweep_detail ?? undefined },
        { label: "Chains", value: `first ${s.expiries} expiries, strikes ±${Math.round(s.moneyness * 100)}%`, fmt: "text" },
      ]} />
      {s.state === "expired" && <div className="warn-box">{s.detail ?? "Questrade refused the stored token."} Paste a new refresh token below.</div>}
      {s.state === "error" && s.detail && <div className="warn-box">{s.detail}</div>}
      {s.paused_until && <p className="muted small">Pausing calls until {new Date(s.paused_until).toLocaleTimeString()} (rate limit).</p>}
      <form className="settings-form" onSubmit={connect}>
        <label>{connected ? "Replace refresh token" : "Refresh token"}
          <input aria-label="Questrade refresh token" type="password" autoComplete="off" spellCheck={false} value={token}
            placeholder="Paste the token from Questrade's API Centre" onChange={(e) => setToken(e.target.value)} />
        </label>
        <div className="toolbar">
          <button className="btn btn-primary" type="submit" disabled={busy || !token.trim()}>{busy ? "Connecting…" : "Connect"}</button>
          {connected && <button className="btn btn-danger" type="button" onClick={disconnect} disabled={busy}>Disconnect</button>}
        </div>
        {msg && (msg.ok ? <InfoBox>{msg.text}</InfoBox> : <ErrorBox error={msg.text} />)}
      </form>
      <Help />
    </Card>
  );
}
