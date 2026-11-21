// The update flow: check → available → downloading → ready → installing. It decides what to do; every
// effect (GitHub, download, staging, the install itself, notifications, the policy) is a dependency, so
// this file has no I/O of its own and is unit-tested with fakes.
//
// Manual mode: the user is told once per new version and installs from Settings or the tray menu.
// Automatic mode: a new version is downloaded in the background, the user is told it is ready, and it is
// installed at a safe moment (tick()) or when the user clicks Restart now. A version that was rolled back
// is never installed automatically.
import { join } from "node:path";
import type { Result, UpdatePhase, UpdateSettings, UpdateStatus } from "../../shared/api";
import type { Logger } from "../log";
import { selectAsset } from "./asset";
import type { DownloadSpec } from "./download";
import type { Location } from "./location";
import type { RecordStore } from "./record";
import type { Release } from "./release";
import { isUpdate } from "./version";

export interface UpdaterDeps {
  currentVersion: string;
  /** The release asset this installation updates from (asset.ts), or null when there is no build for it. */
  assetName(version: string): string | null;
  settings(): UpdateSettings;
  latest(repo: string): Promise<Release>;
  download(spec: DownloadSpec): Promise<void>;
  /** Checks the verified download (identity, version) and prepares it; resolves with the staged path. */
  stage(download: string, stageDir: string, version: string, appPath: string): Promise<string>;
  /** Removes and recreates a folder. */
  resetDir(dir: string): Promise<void>;
  location(): Location;
  /** Safe-window policy: null when the app may restart now. */
  blocker(userInitiated: boolean): string | null;
  /** Records the pending update, starts the helper and quits the app. */
  install(staged: string, appPath: string, version: string): Promise<void>;
  records: RecordStore;
  notify(kind: "available" | "ready", version: string): void;
  onStatus(s: UpdateStatus): void;
  /** Downloads and staging live here, one subfolder per version. */
  workDir: string;
  now(): Date;
  log: Logger;
}

export class Updater {
  private phase: UpdatePhase = "idle";
  private release: Release | null = null;
  private progress: number | null = null;
  private error: string | null = null;
  private blocked: string | null = null;
  private checkedAt: string | null = null;
  private staged: { version: string; app: string } | null = null;
  private installWhenReady = false;
  private repo: string;

  constructor(private readonly d: UpdaterDeps) {
    this.repo = d.settings().source;
  }

  status(): UpdateStatus {
    const rec = this.d.records.get();
    const r = this.release;
    return {
      phase: this.phase, currentVersion: this.d.currentVersion,
      latest: r ? { version: r.version, notes: r.notes, url: r.htmlUrl, publishedAt: r.publishedAt } : null,
      progress: this.progress, error: this.error, blocked: this.blocked,
      skipped: r !== null && rec.skipped.includes(r.version), notice: rec.notice, checkedAt: this.checkedAt,
    };
  }

  private set(phase: UpdatePhase, patch: { error?: string | null; progress?: number | null; blocked?: string | null } = {}): void {
    this.phase = phase;
    if (patch.error !== undefined) this.error = patch.error;
    if (patch.progress !== undefined) this.progress = patch.progress;
    if (patch.blocked !== undefined) this.blocked = patch.blocked;
    this.d.onStatus(this.status());
  }

  private get busy(): boolean {
    return this.phase === "checking" || this.phase === "downloading" || this.phase === "installing";
  }

  async check(): Promise<void> {
    if (this.busy) return;
    const repo = this.d.settings().source;
    this.set("checking", { error: null });
    let r: Release;
    try {
      r = await this.d.latest(repo);
    } catch (e) {
      this.d.log.warn(`update check (${repo}): ${(e as Error).message}`);
      this.set("error", { error: (e as Error).message });
      return;
    }
    this.checkedAt = this.d.now().toISOString();
    this.release = r;
    if (r.draft || r.prerelease || !isUpdate(r.version, this.d.currentVersion)) {
      this.staged = null;
      this.set("up-to-date", { blocked: null });
      return;
    }
    if (this.staged?.version === r.version) { this.set("ready"); return; }
    this.staged = null;
    this.set("available", { blocked: null });
    const auto = this.d.settings().auto && !this.d.records.get().skipped.includes(r.version);
    if (!auto) this.notifyOnce("available", r.version);
    else await this.download();
  }

  /** The user's "Download and install" (Settings) or "Restart now" (notification, tray menu). */
  async installNow(): Promise<Result> {
    if (this.phase === "ready") return this.install(true);
    if (this.phase === "downloading") { this.installWhenReady = true; return { ok: true }; }
    const retry = this.phase === "error" && this.release !== null && isUpdate(this.release.version, this.d.currentVersion);
    if (this.phase !== "available" && !retry) return { ok: false, error: "There is no update to install." };
    this.installWhenReady = true;
    await this.download();
    return this.error ? { ok: false, error: this.error } : this.blocked ? { ok: false, error: this.blocked } : { ok: true };
  }

  /** Periodic: in automatic mode, installs a ready update once the safe-window policy allows it. */
  async tick(): Promise<void> {
    if (this.phase === "ready" && this.d.settings().auto) await this.install(false);
  }

  /** Settings were saved: a different update source starts over with a fresh check. */
  async settingsChanged(): Promise<void> {
    const repo = this.d.settings().source;
    if (repo === this.repo) {
      if (this.d.settings().auto && this.phase === "available") await this.check();   // automatic mode was just turned on
      return;
    }
    this.repo = repo;
    if (this.busy) return;
    this.release = null;
    this.staged = null;
    await this.check();
  }

  dismissNotice(): void {
    this.d.records.set({ ...this.d.records.get(), notice: null });
    this.d.onStatus(this.status());
  }

  private notifyOnce(kind: "available" | "ready", version: string): void {
    const rec = this.d.records.get();
    if (rec.notifiedVersion === version) return;
    this.d.records.set({ ...rec, notifiedVersion: version });
    this.d.notify(kind, version);
  }

  private async download(): Promise<void> {
    const r = this.release;
    if (!r) return;
    const where = this.d.location();
    if (!where.ok) { this.installWhenReady = false; this.set("available", { blocked: where.reason }); return; }
    const sel = selectAsset(r, this.d.assetName(r.version));
    if (!sel.ok) { this.installWhenReady = false; this.set("error", { error: sel.error }); return; }
    this.set("downloading", { progress: 0, error: null, blocked: null });
    const dir = join(this.d.workDir, r.version);
    try {
      await this.d.resetDir(dir);
      const file = join(dir, sel.asset.name);
      await this.d.download({
        url: sel.asset.url, dest: file, sha256: sel.sha256, size: sel.asset.size,
        onProgress: (f) => { this.progress = f; this.d.onStatus(this.status()); },
      });
      const app = await this.d.stage(file, join(dir, "stage"), r.version, where.appPath);
      this.staged = { version: r.version, app };
      this.d.log.info(`update ${r.version} downloaded, verified and staged at ${app}`);
    } catch (e) {
      this.installWhenReady = false;
      this.d.log.warn(`update ${r.version}: ${(e as Error).message}`);
      this.set("error", { error: (e as Error).message, progress: null });
      return;
    }
    this.set("ready", { progress: null });
    if (this.installWhenReady) { this.installWhenReady = false; await this.install(true); }
    else this.notifyOnce("ready", r.version);
  }

  private async install(userInitiated: boolean): Promise<Result> {
    const staged = this.staged;
    if (this.phase !== "ready" || !staged) return { ok: false, error: "No update is ready." };
    const where = this.d.location();
    const blocked = where.ok ? this.d.blocker(userInitiated) : where.reason;
    if (blocked || !where.ok) {
      if (blocked !== this.blocked) this.set("ready", { blocked });
      return { ok: false, error: blocked ?? "" };
    }
    this.set("installing", { blocked: null });
    try {
      await this.d.install(staged.app, where.appPath, staged.version);
      return { ok: true };
    } catch (e) {
      this.d.log.error(`installing update ${staged.version}: ${(e as Error).message}`);
      this.staged = null;
      this.set("error", { error: `The update could not be installed: ${(e as Error).message}` });
      return { ok: false, error: this.error! };
    }
  }
}
