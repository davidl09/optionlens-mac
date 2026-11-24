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

/** What macOS has for "launch at login" ("requires-approval": the user must allow it in System Settings). */
export type LoginItemStatus = "enabled" | "disabled" | "requires-approval" | "unsupported";

export interface UpdateSettings {
  /** Download updates in the background and install them at a safe moment. */
  auto: boolean;
  /** GitHub repository "owner/repo" whose releases are installed. */
  source: string;
}

export interface AppSettings {
  bridge: BridgeSettings;
  launchAtLogin: boolean;
  updates: UpdateSettings;
}

export type UpdatePhase = "idle" | "checking" | "up-to-date" | "available" | "downloading" | "ready" | "installing" | "error";
export interface UpdateStatus {
  phase: UpdatePhase;
  currentVersion: string;
  /** The latest release seen (notes are plain text: never rendered as HTML). */
  latest: { version: string; notes: string; url: string; publishedAt: string | null } | null;
  /** 0..1 while downloading. */
  progress: number | null;
  error: string | null;
  /** Why the update cannot be installed right now (app location, safe-window policy), else null. */
  blocked: string | null;
  /** The latest version was rolled back before and is not installed automatically. */
  skipped: boolean;
  /** A message about an earlier update (rollback, failed install) until dismissed. */
  notice: string | null;
  checkedAt: string | null;
}

/** Bridge work kinds in §6 order: on-demand first, the rest share time by weight (bridge/scheduler.ts); "live":
 * snapshots of the symbols on screen while Questrade is unavailable (INTEGRATION.md §9). */
export const JOB_KINDS = ["on-demand", "live", "eod-chains", "chain-universe", "underlying-stats", "index", "option-bars", "iv-history"] as const;
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
  /**
   * An after-hours fill in progress: the last completed session had no data (e.g. a first launch on a weekend), so
   * the bridge collects its frozen closing values — underlying stats, then chains; null when none runs.
   */
  populate: { sessionDate: string; done: number; total: number } | null;
  /** IBKR's server farms as the Gateway reports them (all "unknown" until it does, and while not connected). */
  farms: Record<"marketData" | "historical" | "secDef", { state: "ok" | "inactive" | "connecting" | "down" | "unknown"; since: string | null; server: string | null }>;
  /**
   * Whether chains can get fresh contract definitions from IBKR now: false while the sec-def farm is reported down,
   * or for 15 min after a contract request went unanswered (chains then use saved contract lists).
   */
  contractsAvailable: boolean;
  /** US options market data of the account: "delayed_only" = no OPRA subscription, option quotes in market hours only. */
  optionsSubscription: "unknown" | "realtime" | "delayed_only";
  /** Whether the last chain's option quotes were delayed (null: no chain fetched yet). */
  optionsDelayed: boolean | null;
  /** When the last chain was pushed (any kind). */
  lastChainAt: string | null;
  /** The last round of scheduled chain work (universe, closing chains, fills) once its queue emptied. */
  lastChainRound: { at: string; detail: string } | null;
  /** Most frequent failure reasons of the last 24 h (at most 3). */
  failures: { reason: string; count: number }[];
  /** Market-data lines used per batch (of the account's limit, Settings → Market data lines). */
  linesPerBatch: number;
}

export type RefreshResult =
  | { status: "refreshed" }
  | { status: "fresh" }                      // pushed within chains_on_demand_max_age_s
  | { status: "no-options" }                 // IBKR lists no options for the symbol (reported to the engine)
  // bridge off / TWS not connected / paused; `waitingFor`: what the bridge waits for before it can fetch the chain
  | { status: "unavailable"; message: string; waitingFor?: "contract-server" | "market-hours" }
  | { status: "failed"; message: string };

/** Intraday bars for the spans the engine could not fill (GET /bars?interval=… `missing`, INTEGRATION.md §5). */
export type IntradayInterval = "1m" | "5m" | "15m" | "1h";
export interface IntradayRequest { symbol: string; interval: IntradayInterval; spans: { start: string; end: string }[] }
export type IntradayResult =
  | { status: "fetched"; bars: number }      // pushed to the engine (POST /api/ingest/intraday-bars)
  | { status: "unavailable"; message: string }   // bridge off / TWS not connected / paused
  | { status: "failed"; message: string };

export type Result = { ok: true } | { ok: false; error: string };

/** A command an MCP client runs to start a stdio server. */
export interface McpLaunch { command: string; args: string[]; launcher: string }
/** How an MCP client starts OptionLens's MCP server on this computer (Settings → AI agents, INTEGRATION.md §10). */
export interface McpSetup {
  command: string;
  args: string[];
  /** The launcher in the app (the AppImage itself on Linux); null in development (the engine's virtualenv Python). */
  launcher: string | null;
  launcherFound: boolean;
  platform: "darwin" | "win32" | "linux";
}

export interface DesktopApi {
  engine(): Promise<EngineStatus>;
  restartEngine(): Promise<void>;
  /** Opens a file picker for an optionlens.duckdb, then stops the engine, copies it in and restarts. */
  importData(): Promise<Result | { ok: false; cancelled: true }>;
  settings(): Promise<AppSettings>;
  saveSettings(s: AppSettings): Promise<Result>;
  loginItemStatus(): Promise<LoginItemStatus>;
  bridgeStatus(): Promise<BridgeStatus>;
  refreshSymbol(symbol: string): Promise<RefreshResult>;
  /** The page that asked for refreshSymbol(symbol) went away: drop the refresh if it has not finished. */
  cancelRefresh(symbol: string): Promise<void>;
  /** IBKR historical bars (TRADES, all hours) for these spans, pushed to the engine; resolves when done. */
  intradayBars(req: IntradayRequest): Promise<IntradayResult>;
  cancelJobs(kind?: JobKind): Promise<void>;
  onEngineStatus(cb: (s: EngineStatus) => void): () => void;
  onBridgeStatus(cb: (s: BridgeStatus) => void): () => void;
  /** In-app navigation requested by the main process (notification click, menu). */
  onNavigate(cb: (path: string) => void): () => void;
  /** Progress of a running importData copy (bytes). */
  onImportProgress(cb: (p: { copied: number; total: number }) => void): () => void;
  updateStatus(): Promise<UpdateStatus>;
  checkForUpdates(): Promise<void>;
  /** Downloads the latest release if needed, then restarts into it (when the safe-window policy allows). */
  installUpdate(): Promise<Result>;
  dismissUpdateNotice(): Promise<void>;
  onUpdateStatus(cb: (s: UpdateStatus) => void): () => void;
  /** The command that starts OptionLens's MCP server, for the setup snippets in Settings → AI agents. */
  mcpSetup(): Promise<McpSetup>;
}

export const IPC = {
  engine: "engine:get",
  restartEngine: "engine:restart",
  importData: "engine:import",
  settings: "settings:get",
  saveSettings: "settings:save",
  loginItemStatus: "settings:login-item",
  bridgeStatus: "bridge:status",
  refreshSymbol: "bridge:refresh",
  cancelRefresh: "bridge:cancel-refresh",
  intradayBars: "bridge:intraday-bars",
  cancelJobs: "bridge:cancel",
  updateStatus: "update:status",
  checkForUpdates: "update:check",
  installUpdate: "update:install",
  dismissUpdateNotice: "update:dismiss-notice",
  mcpSetup: "mcp:setup",
  updateEvent: "event:update",
  engineEvent: "event:engine",
  bridgeEvent: "event:bridge",
  navigateEvent: "event:navigate",
  importProgressEvent: "event:import-progress",
} as const;
