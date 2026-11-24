// Settings → AI agents (MCP, INTEGRATION.md §10): the switch that allows AI agents (default off), what agents did
// (connected clients, last call), "Regenerate key", and copyable setup snippets for Claude Code, Claude Desktop and any
// stdio MCP client, for this computer's paths (from the main process). The key itself is never shown: the app writes
// it to a private connection file that the MCP server reads.
import { useEffect, useState } from "react";
import type { McpSetup } from "../../electron/shared/api";
import { api } from "../api/client";
import type { McpStatus } from "../api/types";
import { desktop } from "../desktop";
import { fmtDateTime } from "../lib/format";
import { MCP_WARNING, mcpSnippets } from "../lib/mcp";
import { Card, ErrorBox, PathText } from "./ui";

const STATUS_POLL_MS = 5_000;

function Snippet({ label, text, hint }: { label: string; text: string; hint?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setCopied(true); window.setTimeout(() => setCopied(false), 1500); } catch { setCopied(false); }
  };
  return (
    <div className="mcp-snippet">
      <div className="mcp-snippet-head"><b>{label}</b>{hint && <span className="muted small">{hint}</span>}
        <button type="button" className="btn btn-sm" onClick={() => void copy()} aria-label={`Copy ${label}`}>{copied ? "Copied" : "Copy"}</button>
      </div>
      <pre className="mcp-code" aria-label={label}>{text}</pre>
    </div>
  );
}

export function mcpStatusText(s: McpStatus): string {
  if (!s.enabled) return "Off: no agent can connect.";
  if (s.clients.length) return `On · connected: ${s.clients.map((c) => c.name).join(", ")}`;
  return s.last_call ? "On · no agent active in the last 10 minutes" : "On · no agent has connected yet";
}

export function McpCard() {
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [setup, setSetup] = useState<McpSetup | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void desktop?.mcpSetup().then((s) => { if (alive) setSetup(s); });
    const poll = () => api.mcpStatus().then((s) => { if (alive) setStatus(s); }, (e: unknown) => { if (alive) setError(e instanceof Error ? e.message : String(e)); });
    void poll();
    const t = setInterval(() => void poll(), STATUS_POLL_MS);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const act = async (fn: () => Promise<McpStatus>) => {
    setBusy(true);
    setError(null);
    try { setStatus(await fn()); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const toggle = () => { if (status) void act(() => api.setMcp(!status.enabled)); };
  const regenerate = () => {
    if (!window.confirm("Regenerate the MCP key? Connected agents lose access until their MCP server reads the new key (it does so by itself on the next call).")) return;
    void act(api.regenerateMcpKey);
  };
  const on = status?.enabled === true;
  const snippets = setup ? mcpSnippets(setup) : null;
  return (
    <Card title="AI agents (MCP)" subtitle={status && <span className={`badge ${on ? "pos" : "neutral"}`}>{on ? "on" : "off"}</span>}>
      <div className="switch-row">
        <button type="button" role="switch" className="switch" aria-checked={on} aria-labelledby="mcp-label" disabled={!status || busy} aria-busy={busy} onClick={toggle}>
          <span className="switch-knob" aria-hidden="true" />
        </button>
        <span id="mcp-label" className="switch-label">Allow AI agents (MCP)</span>
      </div>
      <p className="small warn-text">{MCP_WARNING}</p>
      {on && status && (
        <div className="switch-row">
          <button type="button" role="switch" className="switch" aria-checked={status.portfolio} aria-labelledby="mcp-portfolio-label" disabled={busy} onClick={() => void act(() => api.setMcpPortfolio(!status.portfolio))}>
            <span className="switch-knob" aria-hidden="true" />
          </button>
          <span id="mcp-portfolio-label" className="switch-label">Allow agents to see my portfolio</span>
          <span className="muted small">{status.portfolio ? "On: agents can read positions, P/L and balances (account numbers masked; still no trading)" : "Off: portfolio tools hidden from agents"}</span>
        </div>
      )}
      <p className="muted small">An AI agent on this computer (Claude Code, Claude Desktop, any MCP client) can then use OptionLens like you do: every page's data, reports, what you are looking at, watchlists, alerts and presets. OptionLens must be running.</p>
      {status && (
        <div className="small" role="status" data-testid="mcp-status">
          <div>{mcpStatusText(status)}</div>
          {on && status.last_call && <div className="muted">Last call: {status.last_call.operation} by {status.last_call.client}, {fmtDateTime(status.last_call.at)}</div>}
        </div>
      )}
      {on && (
        <div className="toolbar">
          <button className="btn" onClick={regenerate} disabled={busy}>Regenerate key</button>
          <span className="muted small">Connection file: <PathText path={status.connection_file} /></span>
        </div>
      )}
      {snippets && setup && (
        <div className="mcp-setup">
          <h4>Set up an agent</h4>
          {!setup.launcherFound && <div className="warn-box">The MCP launcher was not found at {setup.launcher ?? setup.command}.</div>}
          <Snippet label="Claude Code" text={snippets.claudeCode} hint="run in a terminal" />
          <Snippet label="Claude Desktop" text={snippets.claudeDesktop} hint={snippets.desktopConfigPath ? `add to ${snippets.desktopConfigPath}` : undefined} />
          <Snippet label="Any MCP client (stdio command)" text={snippets.generic} />
        </div>
      )}
      {error && <ErrorBox error={error} />}
    </Card>
  );
}
