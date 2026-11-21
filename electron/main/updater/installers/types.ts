// How an update is installed on this OS (installers/mac.ts, linux.ts, windows.ts). The updater decides
// what and when (updater.ts); an Installer only knows where the app lives and how to replace it.
import type { Location } from "../location";

export interface Installer {
  /** Where the running app is installed, or why it cannot update itself there. */
  location(): Location;
  /**
   * Checks the downloaded (and already SHA-256-verified) file for identity and version, and prepares it for
   * install(); resolves with the prepared file's path. `stageDir` is an empty scratch folder.
   */
  stage(download: string, stageDir: string, version: string, appPath: string): Promise<string>;
  /**
   * Starts the detached helper that installs `staged` over the app at `appPath` once this process has exited
   * (the caller quits right after). Resolves with where the current version is kept for a rollback.
   */
  install(staged: string, appPath: string, version: string): Promise<string>;
  /** Rollback: starts the helper that puts `backup` back in place of the app once this process has exited. */
  restore(backup: string, appPath: string): Promise<void>;
  /** Whether a backup from install() is still there. */
  hasBackup(backup: string): boolean;
}

/** What every installer gets. */
export interface InstallerBase {
  pid: number;
  currentVersion: string;
  backupsDir: string;
  logFile: string;
}

export const KEEP_BACKUPS = 2;
