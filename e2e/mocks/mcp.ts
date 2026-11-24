// AI-agent access (/api/mcp) and the current view (/api/view, INTEGRATION.md §10), stateful like the engine: the views
// the renderer reported are kept for tests to read; the key is never in an answer.
import type { McpStatus } from "../../src/api/types";
import type { CurrentView } from "../../src/state/view";

let enabled = false;
let portfolio = false;
let regenerated = 0;
export const views: CurrentView[] = [];

export function resetMcp(): void {
  enabled = false;
  portfolio = false;
  regenerated = 0;
  views.length = 0;
}

export const regenerations = (): number => regenerated;

export function status(): McpStatus {
  return {
    enabled, portfolio: enabled && portfolio, connection_file: "/Users/me/Library/Application Support/OptionLens/data/mcp-connection.json", port: enabled ? 59999 : null,
    last_call: enabled ? { at: "2026-11-24T15:02:00-05:00", operation: "GET /api/symbol/{sym}/summary", client: "claude-code/2.1.0" } : null,
    clients: enabled ? [{ name: "claude-code/2.1.0", last_seen_at: "2026-11-24T15:02:00-05:00" }] : [],
  };
}

/** PUT /api/mcp: `enabled` and / or `portfolio` (off resets the portfolio permission, like the engine). */
export function set(body: { enabled?: boolean; portfolio?: boolean }): McpStatus {
  if (body.enabled !== undefined) { enabled = body.enabled; if (!enabled) portfolio = false; }
  if (body.portfolio !== undefined && enabled) portfolio = body.portfolio;
  return status();
}
export function regenerate(): McpStatus | null { if (!enabled) return null; regenerated += 1; return status(); }

export function reportView(v: CurrentView): { ok: boolean } { views.push(v); return { ok: true }; }
export const currentView = () => ({ view: views.at(-1) ?? null, reported_at: null, age_s: null });
