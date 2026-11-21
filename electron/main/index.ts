// Main process composition root: wires the engine sidecar, the IBKR bridge, notifications, the window,
// the tray icon and IPC. Behaviour lives in the modules imported here; this file only connects them. What
// differs per OS comes from platform/ (desktop integration) and updater/installers (updates).
import { app, BrowserWindow, dialog, Menu, Notification, powerMonitor, protocol, session } from "electron";
import { execFile, spawn } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { Bridge } from "../bridge/bridge";
import { ibConnector } from "../bridge/ib-adapter";
import { ingestApi } from "../bridge/ingest";
import { Ledger } from "../bridge/ledger";
import { IPC, type AppSettings, type JobKind } from "../shared/api";
import { backupDatabaseOnVersionChange } from "./db-backup";
import { EngineSupervisor } from "./engine";
import { resolveEngineCommand } from "./engine-paths";
import { createEngineHttp, EngineHttpError } from "./engine-http";
import { importDatabase } from "./import-data";
import { registerIpc } from "./ipc";
import { jsonFile } from "./json-file";
import { createLogger } from "./log";
import { isNotificationList, NotificationPoller, notificationRoute, showRetained, type EngineNotification } from "./notifier";
import { allowPermission } from "./permissions";
import { APP_SCHEME, serveAsset } from "./protocol";
import { launchAtLoginFromOs } from "./login-item";
import { desktopPlatform, type PlatformDeps } from "./platform";
import { parseStoredSettings, validateSettings } from "./settings";
import { AppTray } from "./tray";
import { createAppUpdater } from "./updater";
import { platformInstaller } from "./updater/installers";
import { createMainWindow } from "./window";

protocol.registerSchemesAsPrivileged([{ scheme: APP_SCHEME, privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true } }]);

if (!app.requestSingleInstanceLock()) app.quit();
else void app.whenReady().then(main);

const textFiles: PlatformDeps["files"] = {
  read: (path) => { try { return readFileSync(path, "utf8"); } catch { return null; } },
  write: (path, content) => { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, content, { mode: 0o644 }); },
  remove: (path) => rmSync(path, { force: true }),
};
const execFileAsync = promisify(execFile);
const run: PlatformDeps["run"] = async (file, args, timeout) => (await execFileAsync(file, args, { timeout, maxBuffer: 1024 * 1024 })).stdout;

async function main(): Promise<void> {
  const userData = app.getPath("userData");
  const log = createLogger(join(app.getPath("logs"), "main.log"));
  process.on("uncaughtException", (e) => log.error(`uncaught: ${e.stack ?? e.message}`));
  process.on("unhandledRejection", (e) => log.error(`unhandled rejection: ${e instanceof Error ? e.stack : String(e)}`));

  const platform = desktopPlatform(process.platform, {
    app, argv: process.argv, env: process.env, execPath: process.execPath, packaged: app.isPackaged, home: app.getPath("home"), files: textFiles, run,
  });
  platform.initNotifications();
  // Linux asks the session bus whether a tray can show; the answer is needed before the window decision.
  const trayIcons = platform.trayIcons().catch(() => null);

  const settingsFile = jsonFile(join(userData, "settings.json"));
  const stateFile = jsonFile(join(userData, "state.json"));
  let settings: AppSettings = parseStoredSettings(settingsFile.read());
  const state: { lastNotifiedId: number; lastRunVersion?: string } = { lastNotifiedId: 0, ...(stateFile.read() as { lastNotifiedId?: number } | null ?? {}) };
  const dataDir = join(userData, "data");
  let importing = false;
  const appRoot = app.getAppPath();

  // Deny every permission except Local Network Access for app://optionlens (the renderer → loopback engine).
  session.defaultSession.setPermissionRequestHandler((wc, perm, cb, details) => cb(allowPermission(perm, details.requestingUrl ?? wc.getURL())));
  session.defaultSession.setPermissionCheckHandler((_wc, perm, origin) => allowPermission(perm, origin));
  protocol.handle(APP_SCHEME, (req) => serveAsset(join(appRoot, "dist"), req.url));

  let win: BrowserWindow | null = null;
  let quitting = false;
  const send = (channel: string, payload: unknown) => { if (win && !win.isDestroyed()) win.webContents.send(channel, payload); };
  const showWindow = (): BrowserWindow => {
    // Without a tray, closing the window quits (there would be no way back to a hidden window).
    if (!win || win.isDestroyed()) win = createMainWindow(join(appRoot, "dist-electron", "preload.cjs"), () => quitting || tray === null);
    else { win.show(); win.focus(); }
    return win;
  };
  const navigate = (path: string) => {
    const w = showWindow();
    if (w.webContents.isLoading()) w.webContents.once("did-finish-load", () => send(IPC.navigateEvent, path));
    else send(IPC.navigateEvent, path);
  };

  // ---- notifications (native) ----
  const shownNotifications = new Set<Notification>();
  const notify = (title: string, body: string, onClick: () => void, what: string) => {
    if (!Notification.isSupported()) return;
    showRetained(new Notification({ title, body }), shownNotifications, onClick, (error) => log.warn(`${what} could not be shown: ${error}`));
  };

  // ---- updates ----
  let tray: AppTray | null = null;
  const appUpdater = createAppUpdater({
    userData, logsDir: app.getPath("logs"), currentVersion: app.getVersion(), platform: process.platform, arch: process.arch,
    installer: (paths) => platformInstaller(process.platform, { ...paths, pid: process.pid, currentVersion: app.getVersion(), packaged: app.isPackaged, execPath: process.execPath, env: process.env }),
    settings: () => settings.updates,
    context: () => ({ importing, idleSeconds: powerMonitor.getSystemIdleTime(), windowFocused: BrowserWindow.getFocusedWindow() !== null }),
    notify: (kind, version) => (kind === "available"
      ? notify(`OptionLens ${version} is available`, "Open Settings → Updates to install it.", () => navigate("/settings"), "update notification")
      : notify(`OptionLens ${version} is ready`, "Click to restart now (OptionLens quits and opens again).", () => void updater.installNow(), "update notification")),
    onStatus: (s) => { send(IPC.updateEvent, s); tray?.update(); },
    quit: () => app.quit(),
    log,
  });
  const updater = appUpdater.updater;
  const launch = appUpdater.launchCheck();
  const probation = launch === "rolling-back" ? null : launch;

  // ---- engine ----
  const engine = new EngineSupervisor({
    command: resolveEngineCommand({ packaged: app.isPackaged, resourcesPath: process.resourcesPath, appPath: appRoot, env: process.env, layout: platform }),
    dataDir,
    // stdin is the engine's stop pipe (engine.ts); windowsHide: no console window for python.exe.
    spawn: (cmd, args, opts) => spawn(cmd, args, { ...opts, stdio: ["pipe", "pipe", "pipe"], windowsHide: true }),
    fetch: (url, init) => fetch(url, init),
    parentPid: process.pid,
    log,
    // A new app version: back up the database before its engine first opens it.
    beforeFirstStart: async () => {
      try {
        const backup = await backupDatabaseOnVersionChange({ dataDir, backupsDir: join(userData, "backups"), currentVersion: app.getVersion(), now: new Date(),
          marker: { read: () => state.lastRunVersion ?? null, write: (v) => { state.lastRunVersion = v; stateFile.write(state); } } });
        if (backup) log.info(`database backed up to ${backup} before the first start of ${app.getVersion()}`);
      } finally {
        probation?.engineStarting();
      }
    },
    onStatus: (s) => {
      send(IPC.engineEvent, s);
      tray?.update();
      if (s.state === "running") {
        probation?.engineHealthy();
        void bridge.pollWants(); void notifier.poll();
      }
    },
  });
  const http = createEngineHttp(() => engine.status().connection);

  // ---- bridge ----
  const bridge: Bridge = new Bridge({
    connector: ibConnector,
    ingest: ingestApi(http),
    ledger: new Ledger(jsonFile(join(userData, "bridge-ledger.json"))),
    settings: () => settings.bridge,
    clientName: `optionlens-mac ${app.getVersion()}`,
    log,
    onStatus: (s) => { send(IPC.bridgeEvent, s); tray?.update(); },
  });

  // ---- alert notifications ----
  const showNotification = (n: EngineNotification) => notify(n.title, n.body, () => {
    navigate(notificationRoute(n.link));
    void http.post("/api/notifications/read", { ids: [n.id] }).catch(() => undefined);
  }, `notification ${n.id}`);
  const notifier = new NotificationPoller({
    fetchUnread: async () => {
      const v = await http.get<unknown>("/api/notifications?unread=1");
      if (!isNotificationList(v)) throw new Error("unexpected notifications payload");
      return v;
    },
    show: showNotification,
    lastShownId: () => state.lastNotifiedId,
    setLastShownId: (id) => { state.lastNotifiedId = id; stateFile.write(state); },
    onError: (e) => { if (!(e instanceof EngineHttpError && e.status === 0)) log.warn(`notifications: ${(e as Error).message}`); },
  });

  // ---- settings ----
  const loginItem = () => platform.loginItem.status();
  // The OS is the truth for launch at login (the user may have removed the item outside OptionLens).
  const fromOs = launchAtLoginFromOs(loginItem());
  if (fromOs !== null && fromOs !== settings.launchAtLogin) { settings = { ...settings, launchAtLogin: fromOs }; settingsFile.write(settings); }
  const saveSettings = (next: unknown) => {
    const v = validateSettings(next);
    if (!v.ok) return v;
    const bridgeChanged = JSON.stringify(v.value.bridge) !== JSON.stringify(settings.bridge);
    const loginChanged = v.value.launchAtLogin !== settings.launchAtLogin;
    const updatesChanged = JSON.stringify(v.value.updates) !== JSON.stringify(settings.updates);
    settings = v.value;
    settingsFile.write(settings);
    if (loginChanged) platform.loginItem.set(settings.launchAtLogin);
    if (bridgeChanged) bridge.reconfigure();
    if (updatesChanged) void updater.settingsChanged();
    tray?.update();
    return { ok: true as const };
  };

  registerIpc({
    engine: () => engine.status(),
    restartEngine: () => engine.restart(),
    importData: async () => {
      const pick = await dialog.showOpenDialog({ title: "Import OptionLens data", properties: ["openFile"], filters: [{ name: "DuckDB database", extensions: ["duckdb"] }] });
      if (pick.canceled || !pick.filePaths[0]) return { ok: false, cancelled: true } as const;
      importing = true;
      try {
        const out = await engine.whileStopped(() => importDatabase(pick.filePaths[0], dataDir, { onProgress: (p) => send(IPC.importProgressEvent, p) }));
        log.info(`imported ${pick.filePaths[0]} → ${out.target}${out.backup ? ` (previous kept as ${out.backup})` : ""}`);
        return { ok: true } as const;
      } catch (e) {
        return { ok: false, error: (e as Error).message } as const;
      } finally {
        importing = false;
      }
    },
    settings: () => settings,
    saveSettings,
    loginItemStatus: loginItem,
    bridgeStatus: () => bridge.status(),
    refreshSymbol: (sym) => bridge.refreshSymbol(sym),
    cancelRefresh: (sym) => bridge.cancelRefresh(sym),
    cancelJobs: (kind?: JobKind) => bridge.cancel(kind),
    updateStatus: () => updater.status(),
    checkForUpdates: () => updater.check(),
    installUpdate: () => updater.installNow(),
    dismissUpdateNotice: () => updater.dismissNotice(),
  });

  // ---- menus, tray, window ----
  Menu.setApplicationMenu(Menu.buildFromTemplate(platform.appMenu({ openSettings: () => navigate("/settings") })));
  const icons = await trayIcons;
  if (!icons) log.info("no tray on this desktop: closing the window quits OptionLens");
  tray = icons && new AppTray(join(appRoot, "resources"), icons, {
    open: () => { showWindow(); },
    openSettings: () => navigate("/settings"),
    setBridgeEnabled: (on) => saveSettings({ ...settings, bridge: { ...settings.bridge, enabled: on } }),
    cancelBridgeWork: () => bridge.cancel(),
    setLaunchAtLogin: (on) => saveSettings({ ...settings, launchAtLogin: on }),
    installUpdate: () => void updater.installNow(),
    quit: () => app.quit(),
  }, () => ({ engine: engine.status(), bridge: bridge.status(), loginItem: loginItem(), update: updater.status() }));

  app.on("second-instance", () => { showWindow(); });
  app.on("activate", () => { showWindow(); });
  // With a tray, keep running when the window is closed: collection and the bridge continue.
  app.on("window-all-closed", () => { if (!tray) app.quit(); });
  let shutDown = false;
  app.on("before-quit", (e) => {
    quitting = true;
    if (shutDown) return;
    e.preventDefault();
    probation?.quitting();
    stopUpdates();
    notifier.stop();
    bridge.stop();
    void engine.shutdown().finally(() => { shutDown = true; app.quit(); });
  });

  let stopUpdates: () => void = () => undefined;
  if (launch === "rolling-back") { log.warn("restoring the previous version; not starting the engine"); return; }
  engine.start();
  bridge.start();
  notifier.start();
  stopUpdates = appUpdater.start();
  const notice = updater.status().notice;
  if (notice) notify("OptionLens update", notice, () => navigate("/settings"), "update notice");
  // Started at login: stay in the tray (when there is one).
  if (!(tray && platform.loginItem.openedAtLogin())) showWindow();
  log.info(`OptionLens ${app.getVersion()} started (data: ${dataDir})`);
}
