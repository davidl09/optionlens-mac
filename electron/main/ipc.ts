// IPC handlers for the preload API (electron/shared/api.ts). Only frames served from app://optionlens may
// call them; every argument from the renderer is validated here.
import { ipcMain, type IpcMainInvokeEvent } from "electron";
import { parseIntradayRequest } from "../bridge/intraday";
import { IPC, JOB_KINDS, type AppSettings, type BridgeStatus, type EngineStatus, type IntradayRequest, type IntradayResult, type JobKind, type LoginItemStatus, type McpSetup, type RefreshResult, type Result, type UpdateStatus } from "../shared/api";
import { APP_ORIGIN } from "./protocol";

export interface IpcBackend {
  engine(): EngineStatus;
  restartEngine(): Promise<void>;
  importData(): Promise<Result | { ok: false; cancelled: true }>;
  settings(): AppSettings;
  saveSettings(s: unknown): Result;
  loginItemStatus(): LoginItemStatus;
  bridgeStatus(): BridgeStatus;
  refreshSymbol(symbol: string): Promise<RefreshResult>;
  cancelRefresh(symbol: string): void;
  intradayBars(req: IntradayRequest): Promise<IntradayResult>;
  cancelJobs(kind?: JobKind): void;
  updateStatus(): UpdateStatus;
  checkForUpdates(): Promise<void>;
  installUpdate(): Promise<Result>;
  dismissUpdateNotice(): void;
  mcpSetup(): McpSetup;
}

const SYMBOL_RE = /^[A-Za-z0-9.^_-]{1,15}$/;

function trusted(e: IpcMainInvokeEvent): boolean {
  return (e.senderFrame?.url ?? "").startsWith(`${APP_ORIGIN}/`);
}

export function registerIpc(b: IpcBackend): void {
  const handle = (channel: string, fn: (...args: unknown[]) => unknown) => {
    ipcMain.handle(channel, (e, ...args: unknown[]) => {
      if (!trusted(e)) throw new Error("untrusted sender");
      return fn(...args);
    });
  };
  handle(IPC.engine, () => b.engine());
  handle(IPC.restartEngine, () => b.restartEngine());
  handle(IPC.importData, () => b.importData());
  handle(IPC.settings, () => b.settings());
  handle(IPC.saveSettings, (s) => b.saveSettings(s));
  handle(IPC.loginItemStatus, () => b.loginItemStatus());
  handle(IPC.bridgeStatus, () => b.bridgeStatus());
  handle(IPC.refreshSymbol, (sym) => {
    if (typeof sym !== "string" || !SYMBOL_RE.test(sym)) return { status: "failed", message: "invalid symbol" } satisfies RefreshResult;
    return b.refreshSymbol(sym);
  });
  handle(IPC.cancelRefresh, (sym) => {
    if (typeof sym !== "string" || !SYMBOL_RE.test(sym)) throw new Error("invalid symbol");
    b.cancelRefresh(sym);
  });
  handle(IPC.intradayBars, (req) => {
    const r = parseIntradayRequest(req);
    return r ? b.intradayBars(r) : ({ status: "failed", message: "invalid request" } satisfies IntradayResult);
  });
  handle(IPC.updateStatus, () => b.updateStatus());
  handle(IPC.checkForUpdates, () => b.checkForUpdates());
  handle(IPC.installUpdate, () => b.installUpdate());
  handle(IPC.dismissUpdateNotice, () => b.dismissUpdateNotice());
  handle(IPC.mcpSetup, () => b.mcpSetup());
  handle(IPC.cancelJobs, (kind) => {
    if (kind !== undefined && !(JOB_KINDS as readonly unknown[]).includes(kind)) throw new Error("invalid job kind");
    b.cancelJobs(kind as JobKind | undefined);
  });
}
