// Keeps one TWS / IB Gateway API session alive: auto-detects the port (4001 Gateway live, 7496 TWS live,
// 4002 Gateway paper, 7497 TWS paper, or the configured one), steps the client id when it is in use,
// reconnects with backoff after disconnects (IBKR restarts the Gateway nightly) and turns IBKR's
// connectivity messages into a "paused" state instead of retrying into errors.
import type { BridgeSettings, TwsState } from "../shared/api";
import type { Logger } from "../main/log";
import { portLabel, portsToTry } from "../main/settings";
import { FarmMonitor, type FarmsStatus } from "./farms";
import { CODE, TwsRequestError, type TwsConnector, type TwsSession, type TwsSystemMessage } from "./tws";
import { maskAccount } from "./mask";

export const COMPETING_MESSAGE = "IBKR session in use elsewhere — market data paused";
export const CONNECTIVITY_MESSAGE = "IB Gateway lost its connection to IBKR — market data paused until it is restored";
/** How long a competing-session pause lasts before the bridge tries again (quietly). */
export const COMPETING_PAUSE_MS = 5 * 60_000;
/** Longest connectivity pause: TWS does not always say when it is restored, so the next task then probes. */
export const CONNECTIVITY_PAUSE_MS = 2 * 60_000;
/** "Data farm connection is OK" (market data, HMDS, sec-def): connectivity is back. */
const FARM_OK_CODES = new Set<number>([2104, 2106, 2158]);
const CLIENT_ID_TRIES = 4;

export interface ConnectionState {
  tws: TwsState;
  port: number | null;
  endpoint: string | null;
  accountType: "live" | "paper" | null;
  paused: string | null;
  lastError: string | null;
  /** IBKR's server farms as the Gateway reported them in this session. */
  farms: FarmsStatus;
}

export interface SupervisorOptions {
  connector: TwsConnector;
  settings: () => BridgeSettings;
  log: Logger;
  onSession: (s: TwsSession | null) => void;
  onChange: () => void;
  host?: string;
  connectTimeoutMs?: number;
  now?: () => number;
}

/** Paper accounts are the "D…" (DU…/DF…) ones. */
export function accountType(accounts: string[]): "live" | "paper" | null {
  if (!accounts.length) return null;
  return accounts.every((a) => a.toUpperCase().startsWith("D")) ? "paper" : "live";
}

/** Reconnect delay after `n` consecutive failed rounds: 5 s, 10 s, 20 s, 40 s, then 60 s. */
export function reconnectDelayMs(n: number): number {
  return Math.min(60_000, 5000 * 2 ** Math.max(0, n - 1));
}

export class TwsSupervisor {
  private session: TwsSession | null = null;
  private running = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pauseTimer: ReturnType<typeof setTimeout> | null = null;
  private failedRounds = 0;
  private generation = 0;
  private s: Omit<ConnectionState, "farms"> = { tws: "disabled", port: null, endpoint: null, accountType: null, paused: null, lastError: null };
  private readonly farms: FarmMonitor;

  constructor(private readonly o: SupervisorOptions) {
    this.farms = new FarmMonitor(o.now);
  }

  state(): ConnectionState { return { ...this.s, farms: this.farms.snapshot() }; }

  start(): void {
    if (this.running) return;
    if (!this.o.settings().enabled) { this.update({ tws: "disabled", lastError: null }); return; }
    this.running = true;
    void this.connectRound();
  }

  stop(): void {
    this.running = false;
    this.generation += 1;
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    this.clearPause();
    const s = this.session;
    this.session = null;
    this.farms.reset();
    if (s) { this.o.onSession(null); s.close(); }
    this.update({ tws: "disabled", port: null, endpoint: null, accountType: null, paused: null });
  }

  restart(): void { this.stop(); this.start(); }

  /** Pause data requests (e.g. a task hit a competing session). */
  pause(reason: string, ms: number | null): void {
    this.clearPause();
    if (this.s.paused !== reason) this.o.log.warn(`bridge paused: ${reason}`);
    this.update({ paused: reason });
    if (ms !== null) this.pauseTimer = setTimeout(() => { this.pauseTimer = null; this.update({ paused: null }); }, ms);
  }

  private connectivityRestored(): void {
    if (this.s.paused !== CONNECTIVITY_MESSAGE) return;
    this.o.log.info("bridge: IBKR connectivity restored");
    this.clearPause();
    this.update({ paused: null });
  }

  private clearPause(): void {
    if (this.pauseTimer) { clearTimeout(this.pauseTimer); this.pauseTimer = null; }
  }

  private update(p: Partial<Omit<ConnectionState, "farms">>): void {
    this.s = { ...this.s, ...p };
    this.o.onChange();
  }

  private async connectRound(): Promise<void> {
    const gen = this.generation;
    const settings = this.o.settings();
    this.update({ tws: "connecting" });
    let lastError = "";
    for (const port of portsToTry(settings)) {
      for (let k = 0; k < CLIENT_ID_TRIES; k++) {
        const clientId = settings.clientId + k;
        try {
          const session = await this.o.connector.connect({ host: this.o.host ?? "127.0.0.1", port, clientId, timeoutMs: this.o.connectTimeoutMs ?? 4000 });
          if (gen !== this.generation) { session.close(); return; }
          this.attach(session, port, clientId);
          return;
        } catch (e) {
          if (e instanceof TwsRequestError && e.code === CODE.CLIENT_ID_IN_USE) { lastError = `client id ${clientId} in use`; continue; }
          lastError = (e as Error).message;
          break;
        }
      }
    }
    if (gen !== this.generation) return;
    this.failedRounds += 1;
    const ports = portsToTry(settings).join(", ");
    const error = lastError.includes("in use")
      ? `${COMPETING_MESSAGE} (client ids ${settings.clientId}–${settings.clientId + CLIENT_ID_TRIES - 1} in use)`
      : `No IB Gateway / TWS API on 127.0.0.1 port ${ports} — is it running with the API enabled?`;
    if (this.failedRounds === 1 || this.s.lastError !== error) this.o.log.info(`bridge: ${error} (${lastError})`);
    this.update({ tws: "disconnected", port: null, endpoint: null, accountType: null, lastError: error });
    this.timer = setTimeout(() => { this.timer = null; if (this.running) void this.connectRound(); }, reconnectDelayMs(this.failedRounds));
  }

  private attach(session: TwsSession, port: number, clientId: number): void {
    const gen = this.generation;
    this.session = session;
    this.failedRounds = 0;
    this.farms.reset();
    const endpoint = portLabel(port);
    this.o.log.info(`bridge: connected to ${endpoint} on port ${port} (client id ${clientId}, accounts ${session.accounts.map(maskAccount).join(",") || "?"})`);
    session.onSystemMessage((m) => this.onSystemMessage(m));
    session.onClose((reason) => {
      if (gen !== this.generation || this.session !== session) return;
      this.o.log.info(`bridge: disconnected (${reason})`);
      this.session = null;
      this.clearPause();
      this.farms.reset();
      this.o.onSession(null);
      this.update({ tws: "disconnected", port: null, endpoint: null, accountType: null, paused: null, lastError: `disconnected: ${reason}` });
      this.timer = setTimeout(() => { this.timer = null; if (this.running) void this.connectRound(); }, reconnectDelayMs(1));
    });
    this.update({ tws: "connected", port, endpoint, accountType: accountType(session.accounts), paused: null, lastError: null });
    this.o.onSession(session);
  }

  private onSystemMessage(m: TwsSystemMessage): void {
    const farm = this.farms.apply(m);
    if (farm) {
      const f = this.farms.status(farm);
      this.o.log.info(`bridge: IBKR ${farm} farm ${f.state}${f.server ? ` (${f.server})` : ""}: ${m.code} ${m.message}`);
      this.o.onChange();
    }
    switch (m.code) {
      case CODE.COMPETING_SESSION:
        this.pause(COMPETING_MESSAGE, COMPETING_PAUSE_MS);
        return;
      case CODE.CONNECTIVITY_LOST:
      case 2110:   // connectivity between TWS and server is broken
        this.pause(CONNECTIVITY_MESSAGE, CONNECTIVITY_PAUSE_MS);
        return;
      case CODE.RESTORED_DATA_LOST:
      case CODE.RESTORED_DATA_KEPT:
        this.connectivityRestored();
        return;
      default:
        if (FARM_OK_CODES.has(m.code)) this.connectivityRestored();
        return;
    }
  }
}
