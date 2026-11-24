// Settings → Data sources → Questrade: the source's status (the same fields as IBKR's, SourceSummary), connect with a
// refresh token from Questrade's API Centre (checked at once with one market-data call), disconnect (INTEGRATION.md
// §8); live updates by streaming or polling (LiveModeSwitch, §9). The token is sent once and never shown again.
import { FormEvent, useState } from "react";
import { api } from "../api/client";
import type { QuestradeStatus } from "../api/types";
import { useApi } from "../api/useApi";
import { questradeView } from "../lib/sources";
import { LiveModeSwitch } from "./LiveModeSwitch";
import { PositionsSwitch } from "./PositionsSwitch";
import { SourceStateBadge, SourceSummary } from "./SourceSummary";
import { Card, ErrorBox, InfoBox, Spinner } from "./ui";

function Help() {
  return (
    <details className="muted small">
      <summary>How to get a refresh token</summary>
      <ol>
        <li>Log in to Questrade and open <b>API Centre</b> from the menu under your name (activate the API once and accept the agreement).</li>
        <li>Click <b>Register a personal app</b>, give it a name, enable the permission <b>Retrieve delayed and real-time market data</b> (and, for
          My → Portfolio, <b>Retrieve balances, positions, orders and executions</b>: OptionLens only reads positions and balances; never enable trading), and save.</li>
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

/** The Portfolio's account access: the app needs "Retrieve balances, positions, orders and executions" (read only). */
function AccountAccess({ s }: { s: QuestradeStatus }) {
  const a = s.account_access;
  if (a.state === "needs_permission") return <div className="warn-box" role="alert" data-testid="questrade-account-access">{a.detail}</div>;
  if (a.state === "ok") return <p className="small pos-text" data-testid="questrade-account-access">Account access: positions and balances readable (read-only).</p>;
  if (a.state === "error" && a.detail) return <p className="small warn-text" data-testid="questrade-account-access">Account access: {a.detail}</p>;
  return null;
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
    if (token.trim()) void run(() => api.connectQuestrade(token.trim()), "Token saved.");
  };
  const disconnect = () => {
    if (window.confirm("Disconnect Questrade? The stored token is deleted from this computer (revoke it in Questrade's API Centre too if you no longer need it).")) {
      void run(() => api.disconnectQuestrade(), "Disconnected. The token was deleted.");
    }
  };
  const v = questradeView(s);
  const connected = s.state !== "disconnected";
  return (
    <Card title="Questrade" subtitle={<SourceStateBadge v={v} />}>
      <SourceSummary v={v} />
      {s.verification && (
        <p className={`small ${s.verification.ok ? "pos-text" : "warn-text"}`} role="status">Token check: {s.verification.message}</p>
      )}
      <LiveModeSwitch />
      <PositionsSwitch broker="questrade" />
      <AccountAccess s={s} />
      <p className="muted small">Chains: first {s.expiries} expiries, strikes ±{Math.round(s.moneyness * 100)}%; at most 70% of Questrade's limits.</p>
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
