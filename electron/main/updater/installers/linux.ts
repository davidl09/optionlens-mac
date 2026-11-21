// Linux: OptionLens updates itself when it runs as an AppImage ($APPIMAGE is its path). The verified
// download is checked (appimage.ts), made executable and copied next to the running AppImage (same file
// system, so the final rename is atomic); after the app exits the helper renames it over the AppImage,
// keeps the previous one in backups/ and starts the new one. Not running as an AppImage (an extracted or
// development build): the user is told how to update by hand.
import { chmod, copyFile, readdir, rm } from "node:fs/promises";
import { basename, dirname, isAbsolute, join } from "node:path";
import { checkAppImage, type ExecIn } from "../appimage";
import type { SwapPlan } from "../helper-script";
import { DEVELOPMENT_BUILD, type Location } from "../location";
import { KEEP_BACKUPS, type Installer, type InstallerBase } from "./types";

export const NOT_AN_APPIMAGE = "OptionLens updates itself only when it runs as its AppImage. Install the new version with the one-line installer (install.sh), or download OptionLens-<version>-linux-x64.AppImage from the release page.";

export interface LinuxProbe {
  packaged: boolean;
  /** $APPIMAGE: the running AppImage's path (set by the AppImage runtime). */
  appImage: string | undefined;
  isFile(path: string): boolean;
  writable(path: string): boolean;
}

export function linuxLocation(p: LinuxProbe): Location {
  if (!p.packaged) return { ok: false, reason: DEVELOPMENT_BUILD };
  const file = p.appImage;
  if (!file || !isAbsolute(file) || !p.isFile(file)) return { ok: false, reason: NOT_AN_APPIMAGE };
  if (!p.writable(dirname(file)) || !p.writable(file)) {
    return { ok: false, reason: `OptionLens cannot replace ${file} (no write access there). Move the AppImage to a folder you own, such as ~/.local/bin, or reinstall with the one-line installer.` };
  }
  return { ok: true, appPath: file };
}

/** The staged update's name next to the AppImage (hidden; one at a time). */
export const stagedName = (version: string) => `.OptionLens-${version}.AppImage.update`;
const STAGED_RE = /^\.OptionLens-[0-9A-Za-z.+-]+\.AppImage\.update$/;

export interface LinuxInstallerDeps extends InstallerBase, LinuxProbe {
  execIn: ExecIn;
  swap(plan: Omit<SwapPlan, "workDir" | "archive"> & { archive: string | null }): Promise<void>;
}

export function linuxInstaller(d: LinuxInstallerDeps): Installer {
  return {
    location: () => linuxLocation(d),
    stage: async (download, stageDir, version, appPath) => {
      await chmod(download, 0o755);
      await checkAppImage(d.execIn, download, stageDir, version);
      const dir = dirname(appPath);
      for (const old of (await readdir(dir)).filter((n) => STAGED_RE.test(n))) await rm(join(dir, old), { force: true });
      const staged = join(dir, stagedName(version));
      try {
        await copyFile(download, staged);
        await chmod(staged, 0o755);
      } catch (e) {
        await rm(staged, { force: true });
        throw new Error(`Could not put the update next to ${basename(appPath)}: ${(e as Error).message}`);
      }
      return staged;
    },
    install: async (staged, appPath) => {
      const backup = join(d.backupsDir, `OptionLens-${d.currentVersion}.AppImage`);
      await d.swap({ pid: d.pid, target: appPath, source: staged, archive: backup, prune: { dir: d.backupsDir, keep: KEEP_BACKUPS, suffix: ".AppImage" },
        logFile: d.logFile, open: [appPath], detach: true });
      return backup;
    },
    restore: (backup, appPath) => d.swap({ pid: d.pid, target: appPath, source: backup, archive: null, prune: null, logFile: d.logFile, open: [appPath], detach: true }),
    hasBackup: (backup) => d.isFile(backup),
  };
}
