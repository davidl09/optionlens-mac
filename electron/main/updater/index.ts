// Wires the updater to the real world: GitHub over HTTPS (Node fetch: downloaded files get no quarantine
// flag or Mark-of-the-Web), this OS's installer (installers/: macOS .app swap, Windows NSIS installer, Linux
// AppImage swap), the file system, Electron's idle time and focus state. Also runs the launch check that
// keeps a new version on probation and rolls it back.
import { mkdir, rm } from "node:fs/promises";
import { join } from "node:path";
import type { UpdateSettings, UpdateStatus } from "../../shared/api";
import { jsonFile } from "../json-file";
import type { Logger } from "../log";
import { updateAssetName } from "./asset";
import { downloadVerified, type StreamGet } from "./download";
import type { Installer } from "./installers/types";
import { parseUpdateRecord, type RecordStore, type UpdateRecord } from "./record";
import { isCachedResponse, ReleaseSource, type HttpGet } from "./release";
import { onLaunch, Probation, rollbackImpossible, rollbackStarted, confirmed } from "./rollback";
import { installBlocker, type InstallContext } from "./safe-window";
import { scheduleUpdates } from "./scheduler";
import { Updater } from "./updater";

export interface AppUpdaterOptions {
  userData: string;
  logsDir: string;
  currentVersion: string;
  platform: NodeJS.Platform;
  arch: string;
  /** This OS's installer (installers/index.ts platformInstaller). */
  installer: (paths: { backupsDir: string; logFile: string }) => Installer;
  settings: () => UpdateSettings;
  /** Idle time, focus and import state for the safe-window policy. */
  context: () => Omit<InstallContext, "now">;
  notify: (kind: "available" | "ready", version: string) => void;
  onStatus: (s: UpdateStatus) => void;
  /** Quits the app (the engine is stopped by the normal quit path). */
  quit: () => void;
  log: Logger;
}

export function createAppUpdater(o: AppUpdaterOptions) {
  const backupsDir = join(o.userData, "backups");
  const workDir = join(o.userData, "updates");
  const logFile = join(o.logsDir, "updater.log");
  const recordFile = jsonFile(join(o.userData, "update-state.json"));
  const records: RecordStore = { get: () => parseUpdateRecord(recordFile.read()), set: (r: UpdateRecord) => recordFile.write(r) };
  const cacheFile = jsonFile(join(o.userData, "update-cache.json"));
  const userAgent = `OptionLens/${o.currentVersion}`;

  const http: HttpGet = async (url, headers) => fetch(url, { headers, signal: AbortSignal.timeout(30_000) });
  const streamGet: StreamGet = async (url) => {
    const res = await fetch(url, { redirect: "manual", headers: { "User-Agent": userAgent }, signal: AbortSignal.timeout(30 * 60_000) });
    return { status: res.status, headers: res.headers, body: res.body as unknown as AsyncIterable<Uint8Array> | null };
  };
  const source = new ReleaseSource(http, {
    read: () => { const v = cacheFile.read(); return isCachedResponse(v) ? v : null; },
    write: (c) => cacheFile.write(c),
  }, userAgent);
  const installer = o.installer({ backupsDir, logFile });
  const location = () => installer.location();

  const updater = new Updater({
    currentVersion: o.currentVersion,
    assetName: (version) => updateAssetName(o.platform, o.arch, version),
    settings: o.settings,
    latest: (repo) => source.latest(repo),
    download: (spec) => downloadVerified(streamGet, spec),
    stage: (file, stageDir, version, appPath) => installer.stage(file, stageDir, version, appPath),
    resetDir: async (dir) => { await rm(dir, { recursive: true, force: true }); await mkdir(dir, { recursive: true }); },
    location,
    blocker: (user) => installBlocker({ ...o.context(), now: new Date() }, user),
    install: async (staged, appPath, version) => {
      const backupPath = await installer.install(staged, appPath, version);
      // The helper waits for this process to exit, so recording the update on probation here is in time.
      const rec = records.get();
      records.set({ ...rec, skipped: rec.skipped.filter((v) => v !== version),
        pending: { version, previousVersion: o.currentVersion, backupPath, launches: 0, rollingBack: false } });
      o.log.info(`installing update ${version}: helper started, quitting`);
      o.quit();
    },
    records,
    notify: o.notify,
    onStatus: o.onStatus,
    workDir,
    now: () => new Date(),
    log: o.log,
  });

  /** Restores the previous bundle (once: the record's rollingBack flag guards against loops) and quits. */
  const rollBack = async (): Promise<void> => {
    const rec = records.get();
    const p = rec.pending;
    if (!p) return;
    const where = location();
    const backupOk = installer.hasBackup(p.backupPath);
    if (!where.ok || !backupOk) {
      const why = !backupOk ? `the backup ${p.backupPath} is missing.` : where.ok ? "" : where.reason;
      o.log.error(`rollback of ${p.version} impossible: ${why}`);
      records.set(rollbackImpossible(rec, why));
      o.onStatus(updater.status());
      return;
    }
    records.set(rollbackStarted(rec));
    o.log.warn(`OptionLens ${p.version} did not start correctly: restoring ${p.previousVersion} from ${p.backupPath}`);
    await installer.restore(p.backupPath, where.appPath);
    o.quit();
  };

  return {
    updater,
    /**
     * Call once at launch, before the engine starts. "rolling-back": do not start the engine (the app
     * quits). A Probation: feed it the engine's progress and the quit. null: nothing on probation.
     */
    launchCheck(): "rolling-back" | Probation | null {
      const { rec, decision } = onLaunch(records.get(), o.currentVersion, logFile);
      records.set(rec);
      if (decision.kind === "rollback") {
        void rollBack().catch((e: unknown) => o.log.error(`rollback: ${(e as Error).message}`));
        return "rolling-back";
      }
      if (decision.kind !== "watch") return null;
      o.log.info(`OptionLens ${o.currentVersion} is on probation after an update`);
      const probation = new Probation({
        onConfirm: () => { records.set(confirmed(records.get())); o.log.info(`update to ${o.currentVersion} confirmed`); },
        onRollback: () => { void rollBack().catch((e: unknown) => o.log.error(`rollback: ${(e as Error).message}`)); },
      });
      probation.start();
      return probation;
    },
    /** Clears leftovers of earlier downloads and starts the check schedule; returns a stop function. */
    start(): () => void {
      let stop: (() => void) | null = null;
      let stopped = false;
      void rm(workDir, { recursive: true, force: true }).catch(() => undefined)
        .then(() => { if (!stopped) stop = scheduleUpdates(updater); });
      return () => { stopped = true; stop?.(); };
    },
  };
}
