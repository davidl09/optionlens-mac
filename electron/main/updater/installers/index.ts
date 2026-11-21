// The installer for this OS, with its real dependencies (processes, file system). The one place in the
// updater that picks by OS; other OSes fail fast (the app does not run there).
import { execFile, spawn } from "node:child_process";
import { accessSync, constants, lstatSync } from "node:fs";
import { copyFile, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { withoutAppImageEnv } from "../../appimage-env";
import { macLocation } from "../location";
import type { Exec } from "../stage";
import { launchSwap, type SwapDeps } from "../swap";
import { launchWindowsHelper } from "../windows-helper";
import { linuxInstaller } from "./linux";
import { macInstaller } from "./mac";
import type { Installer, InstallerBase } from "./types";
import { windowsInstaller } from "./windows";

export interface InstallerContext extends InstallerBase {
  packaged: boolean;
  execPath: string;
  env: NodeJS.ProcessEnv;
}

const run = promisify(execFile);
const exec: Exec = async (file, args) => (await run(file, args, { maxBuffer: 1024 * 1024 })).stdout;
const writable = (p: string) => { try { accessSync(p, constants.W_OK); return true; } catch { return false; } };
const kind = (p: string) => { try { return lstatSync(p); } catch { return null; } };
const isFile = (p: string) => kind(p)?.isFile() === true;
const isDir = (p: string) => kind(p)?.isDirectory() === true;
const makeTempDir = () => mkdtemp(join(tmpdir(), "optionlens-update-"));

function posixSwapDeps(env: NodeJS.ProcessEnv): SwapDeps {
  return {
    makeTempDir,
    writeFile: (path, content, mode) => writeFile(path, content, { mode }),
    spawnDetached: (file, args) => { spawn(file, args, { detached: true, stdio: "ignore", env }).unref(); },
  };
}

export function platformInstaller(os: NodeJS.Platform, c: InstallerContext): Installer {
  const base: InstallerBase = { pid: c.pid, currentVersion: c.currentVersion, backupsDir: c.backupsDir, logFile: c.logFile };
  switch (os) {
    case "darwin": {
      const deps = posixSwapDeps(c.env);
      return macInstaller({ ...base, exec, isDir, swap: (plan) => launchSwap(plan, deps),
        location: () => macLocation({ packaged: c.packaged, execPath: c.execPath, writable }) });
    }
    case "linux": {
      const env = withoutAppImageEnv(c.env);
      const deps = posixSwapDeps(env);
      return linuxInstaller({ ...base, packaged: c.packaged, appImage: c.env.APPIMAGE, isFile, writable, swap: (plan) => launchSwap(plan, deps),
        execIn: async (file, args, cwd) => (await run(file, args, { cwd, env, timeout: 120_000, maxBuffer: 1024 * 1024 })).stdout });
    }
    case "win32": {
      const powershell = join(c.env.SystemRoot ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
      return windowsInstaller({ ...base, packaged: c.packaged, execPath: c.execPath, isFile, writable,
        launchHelper: (plan) => launchWindowsHelper(plan, {
          makeTempDir, copyFile, powershell,
          writeFile: (path, content) => writeFile(path, content, "utf8"),
          spawnDetached: (file, args) => { spawn(file, args, { detached: true, stdio: "ignore", windowsHide: true }).unref(); },
        }) });
    }
    default: throw new Error(`OptionLens does not run on ${os}.`);
  }
}
