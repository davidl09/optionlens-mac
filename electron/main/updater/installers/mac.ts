// macOS: the release zip is extracted with ditto and checked (bundle id, version, signature: stage.ts);
// the helper swaps the .app bundle, keeps the previous one in backups/ and opens the app again.
import { join } from "node:path";
import type { Location } from "../location";
import type { SwapPlan } from "../helper-script";
import { stageBundle, type Exec } from "../stage";
import { KEEP_BACKUPS, type Installer, type InstallerBase } from "./types";

const OPEN = "/usr/bin/open";

export interface MacInstallerDeps extends InstallerBase {
  location(): Location;
  exec: Exec;
  /** launchSwap with its real dependencies. */
  swap(plan: Omit<SwapPlan, "workDir" | "archive"> & { archive: string | null }): Promise<void>;
  isDir(path: string): boolean;
}

export function macInstaller(d: MacInstallerDeps): Installer {
  return {
    location: d.location,
    stage: (zip, stageDir, version) => stageBundle(d.exec, zip, stageDir, version),
    install: async (staged, appPath) => {
      const backup = join(d.backupsDir, `OptionLens-${d.currentVersion}.app`);
      await d.swap({ pid: d.pid, target: appPath, source: staged, archive: backup, prune: { dir: d.backupsDir, keep: KEEP_BACKUPS, suffix: ".app" },
        logFile: d.logFile, open: [OPEN, appPath], detach: false });
      return backup;
    },
    restore: (backup, appPath) => d.swap({ pid: d.pid, target: appPath, source: backup, archive: null, prune: null, logFile: d.logFile, open: [OPEN, appPath], detach: false }),
    hasBackup: (backup) => d.isDir(backup),
  };
}
