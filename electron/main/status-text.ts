// Human-readable status lines for the menu-bar icon (pure, so they are unit-tested).
import type { BridgeStatus, EngineStatus } from "../shared/api";

export function engineLine(e: EngineStatus): string {
  switch (e.state) {
    case "running": return `Engine: running${e.version ? ` (v${e.version})` : ""}`;
    case "starting": return "Engine: starting…";
    case "restarting": return `Engine: restarting…${e.lastError ? ` (${e.lastError.slice(0, 60)})` : ""}`;
    case "failed": return `Engine: failing — ${(e.lastError ?? "see log").slice(0, 70)}`;
    case "stopped": return "Engine: stopped";
  }
}

const hhmm = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export function bridgeLines(b: BridgeStatus): string[] {
  if (!b.enabled) return ["IBKR: bridge off"];
  const lines: string[] = [];
  if (b.tws === "connected") {
    lines.push(`IBKR: ${b.endpoint ?? `port ${b.port}`} connected${b.accountType ? ` · ${b.accountType} account` : ""}`);
  } else if (b.tws === "connecting") {
    lines.push("IBKR: connecting…");
  } else {
    lines.push("IBKR: not connected — start IB Gateway");
  }
  if (b.paused) lines.push(b.paused);
  lines.push(...b.notes);
  lines.push(`Last push: ${b.lastPushAt ? hhmm(b.lastPushAt) : "none yet"}`);
  if (b.current) lines.push(`Working: ${b.current.label}`);
  if (b.backfill.total > 0) lines.push(`Backfill: ${b.backfill.done + b.backfill.failed}/${b.backfill.total}${b.backfill.failed ? ` (${b.backfill.failed} failed)` : ""}`);
  return lines;
}

export function trayTooltip(e: EngineStatus, b: BridgeStatus): string {
  return [engineLine(e), bridgeLines(b)[0]].join("\n");
}
