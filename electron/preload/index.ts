// Preload (sandboxed, context-isolated): exposes the typed DesktopApi as window.optionlens. Nothing else
// from Electron or Node reaches the renderer.
import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";
import { IPC, type DesktopApi } from "../shared/api";

function subscribe<T>(channel: string, cb: (v: T) => void): () => void {
  const listener = (_e: IpcRendererEvent, v: T) => cb(v);
  ipcRenderer.on(channel, listener);
  return () => { ipcRenderer.removeListener(channel, listener); };
}

const api: DesktopApi = {
  engine: () => ipcRenderer.invoke(IPC.engine),
  restartEngine: () => ipcRenderer.invoke(IPC.restartEngine),
  importData: () => ipcRenderer.invoke(IPC.importData),
  settings: () => ipcRenderer.invoke(IPC.settings),
  saveSettings: (s) => ipcRenderer.invoke(IPC.saveSettings, s),
  loginItemStatus: () => ipcRenderer.invoke(IPC.loginItemStatus),
  bridgeStatus: () => ipcRenderer.invoke(IPC.bridgeStatus),
  refreshSymbol: (symbol) => ipcRenderer.invoke(IPC.refreshSymbol, symbol),
  cancelRefresh: (symbol) => ipcRenderer.invoke(IPC.cancelRefresh, symbol),
  intradayBars: (req) => ipcRenderer.invoke(IPC.intradayBars, req),
  cancelJobs: (kind) => ipcRenderer.invoke(IPC.cancelJobs, kind),
  onEngineStatus: (cb) => subscribe(IPC.engineEvent, cb),
  onBridgeStatus: (cb) => subscribe(IPC.bridgeEvent, cb),
  onNavigate: (cb) => subscribe(IPC.navigateEvent, cb),
  onImportProgress: (cb) => subscribe(IPC.importProgressEvent, cb),
  updateStatus: () => ipcRenderer.invoke(IPC.updateStatus),
  checkForUpdates: () => ipcRenderer.invoke(IPC.checkForUpdates),
  installUpdate: () => ipcRenderer.invoke(IPC.installUpdate),
  dismissUpdateNotice: () => ipcRenderer.invoke(IPC.dismissUpdateNotice),
  onUpdateStatus: (cb) => subscribe(IPC.updateEvent, cb),
  mcpSetup: () => ipcRenderer.invoke(IPC.mcpSetup),
};

contextBridge.exposeInMainWorld("optionlens", api);
