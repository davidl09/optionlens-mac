// Contract between the main process, the preload bridge and the renderer.
// Types only plus the IPC channel names: no runtime dependencies, safe to import from all three.

/** Where the renderer reaches the local engine. The token lives in memory only (never persisted). */
export interface EngineConnection { baseUrl: string; token: string }

export type EngineState = "starting" | "running" | "restarting" | "stopped" | "failed";
export interface EngineStatus {
  state: EngineState;
  connection: EngineConnection | null;   // non-null only while running
  version: string | null;                // from /api/health
  pid: number | null;
  restarts: number;                      // since app start
  lastError: string | null;
  dataDir: string;
}

export interface BridgeSettings {
  enabled: boolean;
  port: number | null;          // null = auto: 4001 Gateway live, 7496 TWS live, 4002 Gateway paper, 7497 TWS paper
  clientId: number;
  marketDataLines: number;      // the account's simultaneous market-data line limit
}

export interface AppSettings {
  bridge: BridgeSettings;
  launchAtLogin: boolean;
}

/** Bridge work kinds in priority order (highest first), per INTEGRATION.md §6. */
export const JOB_KINDS = ["on-demand", "eod-chains", "chain-universe", "underlying-stats", "index", "option-bars", "iv-history"] as const;
export type JobKind = (typeof JOB_KINDS)[number];

export type TwsState = "disabled" | "connecting" | "connected" | "disconnected";
export interface BridgeStatus {
  enabled: boolean;
  tws: TwsState;
  port: number | null;
  /** e.g. "IB Gateway (live)" for the port that connected. */
  endpoint: string | null;
  accountType: "live" | "paper" | null;
  /** Why data requests are paused while connected (competing session, IBKR servers unreachable), else null. */
  paused: string | null;
  lastError: string | null;
  /** Data-quality notes, e.g. "IBKR delayed (no OPRA subscription)". */
  notes: string[];
  lastPushAt: string | null;
  current: { kind: JobKind; label: string } | null;
  queued: Record<JobKind, number>;
  /** Progress of the current round of historical work (option bars + IV history). */
  backfill: { total: number; done: number; failed: number };
}

export type RefreshResult =
  | { status: "refreshed" }
  | { status: "fresh" }                      // pushed within chains_on_demand_max_age_s
  | { status: "unavailable"; message: string }  // bridge off / TWS not connected / paused
  | { status: "failed"; message: string };

export type Result = { ok: true } | { ok: false; error: string };

export interface DesktopApi {
  engine(): Promise<EngineStatus>;
  restartEngine(): Promise<void>;
  /** Opens a file picker for an optionlens.duckdb, then stops the engine, copies it in and restarts. */
  importData(): Promise<Result | { ok: false; cancelled: true }>;
  settings(): Promise<AppSettings>;
  saveSettings(s: AppSettings): Promise<Result>;
  bridgeStatus(): Promise<BridgeStatus>;
  refreshSymbol(symbol: string): Promise<RefreshResult>;
  cancelJobs(kind?: JobKind): Promise<void>;
  onEngineStatus(cb: (s: EngineStatus) => void): () => void;
  onBridgeStatus(cb: (s: BridgeStatus) => void): () => void;
  /** In-app navigation requested by the main process (notification click, menu). */
  onNavigate(cb: (path: string) => void): () => void;
}

export const IPC = {
  engine: "engine:get",
  restartEngine: "engine:restart",
  importData: "engine:import",
  settings: "settings:get",
  saveSettings: "settings:save",
  bridgeStatus: "bridge:status",
  refreshSymbol: "bridge:refresh",
  cancelJobs: "bridge:cancel",
  engineEvent: "event:engine",
  bridgeEvent: "event:bridge",
  navigateEvent: "event:navigate",
} as const;
