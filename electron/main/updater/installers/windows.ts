// Windows: OptionLens updates itself when it was installed with its NSIS installer (per user, in
// %LOCALAPPDATA%\Programs\OptionLens, next to "Uninstall OptionLens.exe"); a copy from the portable zip is
// updated by hand. The verified setup.exe is checked (version resource: ProductName and ProductVersion,
// pe-version.ts); a copy is kept in backups/ so a later update can roll back to this version. After the
// app exits, the helper (windows-helper.ts) runs the installer silently and starts OptionLens again. Rollback
// runs the previous version's installer the same way.
import { copyFile, mkdir, open, readdir, rm } from "node:fs/promises";
import { join, win32 } from "node:path";
import { PRODUCT_NAME } from "../../app-id";
import { windowsSetupName } from "../asset";
import { DEVELOPMENT_BUILD, type Location } from "../location";
import { readPeVersion } from "../pe-version";
import type { WindowsHelperPlan } from "../windows-helper";
import type { Installer, InstallerBase } from "./types";

export const UNINSTALLER = `Uninstall ${PRODUCT_NAME}.exe`;
export const NOT_INSTALLED = "This copy of OptionLens was not installed with its installer, so it cannot update itself. Install it with the one-line installer (install.ps1), or run OptionLens-<version>-win-x64-setup.exe from the release page.";

export interface WindowsProbe {
  packaged: boolean;
  execPath: string;
  isFile(path: string): boolean;
  writable(path: string): boolean;
}

export function windowsLocation(p: WindowsProbe): Location {
  if (!p.packaged) return { ok: false, reason: DEVELOPMENT_BUILD };
  const dir = win32.dirname(p.execPath);
  if (!p.isFile(win32.join(dir, UNINSTALLER))) return { ok: false, reason: NOT_INSTALLED };
  if (!p.writable(dir)) return { ok: false, reason: `OptionLens cannot write to ${dir} (installed for all users?). Reinstall it for your user with the one-line installer.` };
  return { ok: true, appPath: p.execPath };
}

/** The version resource sits in the installer's first megabytes (the NSIS payload follows). */
const HEAD_BYTES = 16 * 1024 * 1024;

async function readHead(path: string): Promise<Buffer> {
  const f = await open(path, "r");
  try {
    const buf = Buffer.alloc(Math.min(HEAD_BYTES, (await f.stat()).size));
    await f.read(buf, 0, buf.length, 0);
    return buf;
  } finally { await f.close(); }
}

/** null when the installer is OptionLens at `version`, else why not. */
export function setupMismatch(head: Buffer, version: string): string | null {
  const v = readPeVersion(head);
  if (!v) return "The download is not a Windows installer with version information.";
  if (v.strings.ProductName !== PRODUCT_NAME) return `The downloaded installer is "${v.strings.ProductName ?? "(unnamed)"}", not ${PRODUCT_NAME}.`;
  if (v.strings.ProductVersion !== version) return `The downloaded installer is version ${v.strings.ProductVersion || "(none)"}, not ${version}.`;
  return null;
}

export interface WindowsInstallerDeps extends InstallerBase, WindowsProbe {
  /** launchWindowsHelper with its real dependencies. */
  launchHelper(plan: Omit<WindowsHelperPlan, "workDir">): Promise<void>;
}

const SETUP_RE = /^OptionLens-[0-9A-Za-z.+-]+-win-x64-setup\.exe$/;

export function windowsInstaller(d: WindowsInstallerDeps): Installer {
  const backupOf = (version: string) => join(d.backupsDir, windowsSetupName(version));
  return {
    location: () => windowsLocation(d),
    stage: async (download, _stageDir, version) => {
      const mismatch = setupMismatch(await readHead(download), version);
      if (mismatch) throw new Error(mismatch);
      return download;
    },
    install: async (staged, appPath, version) => {
      // Keep this version's installer (a later update can roll back to it) and the current one; drop older ones.
      await mkdir(d.backupsDir, { recursive: true });
      await copyFile(staged, backupOf(version));
      const keep = new Set([windowsSetupName(version), windowsSetupName(d.currentVersion)]);
      for (const n of (await readdir(d.backupsDir)).filter((n) => SETUP_RE.test(n) && !keep.has(n))) await rm(join(d.backupsDir, n), { force: true });
      const previous = backupOf(d.currentVersion);
      await d.launchHelper({ pid: d.pid, installer: staged, fallback: d.isFile(previous) ? previous : null, app: appPath, logFile: d.logFile });
      return previous;
    },
    restore: (backup, appPath) => d.launchHelper({ pid: d.pid, installer: backup, fallback: null, app: appPath, logFile: d.logFile }),
    hasBackup: (backup) => d.isFile(backup),
  };
}
