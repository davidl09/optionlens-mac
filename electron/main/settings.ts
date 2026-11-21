// App settings: defaults, validation of user input (fail fast with a message) and tolerant parsing
// of the stored file (unknown/invalid fields fall back to defaults so a bad file never bricks the app).
import type { AppSettings, BridgeSettings, UpdateSettings } from "../shared/api";
import { DEFAULT_UPDATE_REPO, isRepoSlug } from "./updater/repo";

export const DEFAULT_BRIDGE: BridgeSettings = { enabled: true, port: null, clientId: 17, marketDataLines: 100 };
export const DEFAULT_UPDATES: UpdateSettings = { auto: false, source: DEFAULT_UPDATE_REPO };
export const DEFAULT_SETTINGS: AppSettings = { bridge: DEFAULT_BRIDGE, launchAtLogin: false, updates: DEFAULT_UPDATES };

/** Ports tried in order when no explicit port is configured. */
export const AUTO_PORTS = [4001, 7496, 4002, 7497] as const;

/** What usually listens on a port (defaults of IB Gateway / TWS). */
export function portLabel(port: number): string {
  switch (port) {
    case 4001: return "IB Gateway (live)";
    case 4002: return "IB Gateway (paper)";
    case 7496: return "TWS (live)";
    case 7497: return "TWS (paper)";
    default: return `port ${port}`;
  }
}

type Check<T> = { ok: true; value: T } | { ok: false; error: string };

const isInt = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v);
const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

export function validateBridgeSettings(v: unknown): Check<BridgeSettings> {
  if (!isObj(v)) return { ok: false, error: "Bridge settings must be an object." };
  if (typeof v.enabled !== "boolean") return { ok: false, error: "“Enabled” must be true or false." };
  if (v.port !== null && !(isInt(v.port) && v.port >= 1 && v.port <= 65535)) return { ok: false, error: "Port must be 1–65535, or automatic." };
  if (!(isInt(v.clientId) && v.clientId >= 0 && v.clientId <= 2_147_483_647)) return { ok: false, error: "Client ID must be a whole number ≥ 0." };
  if (!(isInt(v.marketDataLines) && v.marketDataLines >= 5 && v.marketDataLines <= 10_000)) return { ok: false, error: "Market data lines must be 5–10000." };
  return { ok: true, value: { enabled: v.enabled, port: v.port as number | null, clientId: v.clientId, marketDataLines: v.marketDataLines } };
}

export function validateUpdateSettings(v: unknown): Check<UpdateSettings> {
  if (!isObj(v)) return { ok: false, error: "Update settings must be an object." };
  if (typeof v.auto !== "boolean") return { ok: false, error: "“Install updates automatically” must be true or false." };
  const source = typeof v.source === "string" ? v.source.trim() : v.source;
  if (!isRepoSlug(source)) return { ok: false, error: "The update source must be a GitHub repository written as owner/repo." };
  return { ok: true, value: { auto: v.auto, source } };
}

export function validateSettings(v: unknown): Check<AppSettings> {
  if (!isObj(v)) return { ok: false, error: "Settings must be an object." };
  const bridge = validateBridgeSettings(v.bridge);
  if (!bridge.ok) return bridge;
  if (typeof v.launchAtLogin !== "boolean") return { ok: false, error: "“Launch at login” must be true or false." };
  const updates = validateUpdateSettings(v.updates);
  if (!updates.ok) return updates;
  return { ok: true, value: { bridge: bridge.value, launchAtLogin: v.launchAtLogin, updates: updates.value } };
}

/** Stored file → settings; each section independently falls back to its default when invalid. */
export function parseStoredSettings(raw: unknown): AppSettings {
  const o = isObj(raw) ? raw : {};
  const bridge = validateBridgeSettings(isObj(o.bridge) ? { ...DEFAULT_BRIDGE, ...o.bridge } : DEFAULT_BRIDGE);
  const updates = validateUpdateSettings(isObj(o.updates) ? { ...DEFAULT_UPDATES, ...o.updates } : DEFAULT_UPDATES);
  return {
    bridge: bridge.ok ? bridge.value : DEFAULT_BRIDGE,
    launchAtLogin: typeof o.launchAtLogin === "boolean" ? o.launchAtLogin : DEFAULT_SETTINGS.launchAtLogin,
    updates: updates.ok ? updates.value : DEFAULT_UPDATES,
  };
}

/** TWS ports to try for the given settings. */
export function portsToTry(s: BridgeSettings): number[] {
  return s.port === null ? [...AUTO_PORTS] : [s.port];
}

/** Market-data lines one batch may use: leave headroom for the user's own TWS windows. */
export function batchLines(s: BridgeSettings): number {
  return Math.max(1, s.marketDataLines - Math.max(5, Math.ceil(s.marketDataLines * 0.1)));
}
