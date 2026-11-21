import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { execFile } from "node:child_process";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import { afterEach, describe, expect, it, vi } from "vitest";
import { checkAppImage } from "../appimage";
import { peFile, versionResource } from "../pe-fixtures.test-helper";
import { linuxInstaller, linuxLocation, NOT_AN_APPIMAGE, stagedName } from "./linux";
import { macInstaller } from "./mac";
import { NOT_INSTALLED, setupMismatch, UNINSTALLER, windowsInstaller, windowsLocation } from "./windows";
import { withoutAppImageEnv } from "../../appimage-env";

const dirs: string[] = [];
const tempDir = (name = "ol-inst ") => { const d = mkdtempSync(join(tmpdir(), name)); dirs.push(d); return d; };
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }); });
const base = (root: string) => ({ pid: 4242, currentVersion: "0.1.3", backupsDir: join(root, "backups"), logFile: join(root, "updater.log") });

describe("macOS installer", () => {
  it("swaps the bundle, keeping the current one as backups/OptionLens-<current>.app, and restores it on rollback", async () => {
    const swap = vi.fn(async () => undefined);
    const i = macInstaller({ ...base("/u"), exec: async () => "", isDir: () => true, swap, location: () => ({ ok: true, appPath: "/Applications/OptionLens.app" }) });
    expect(await i.install("/u/updates/0.1.4/stage/OptionLens.app", "/Applications/OptionLens.app", "0.1.4")).toBe("/u/backups/OptionLens-0.1.3.app");
    expect(swap).toHaveBeenLastCalledWith({ pid: 4242, target: "/Applications/OptionLens.app", source: "/u/updates/0.1.4/stage/OptionLens.app", archive: "/u/backups/OptionLens-0.1.3.app",
      prune: { dir: "/u/backups", keep: 2, suffix: ".app" }, logFile: "/u/updater.log", open: ["/usr/bin/open", "/Applications/OptionLens.app"], detach: false });
    await i.restore("/u/backups/OptionLens-0.1.3.app", "/Applications/OptionLens.app");
    expect(swap).toHaveBeenLastCalledWith(expect.objectContaining({ source: "/u/backups/OptionLens-0.1.3.app", archive: null, prune: null }));
  });
});

describe("Linux installer", () => {
  const probe = { packaged: true, appImage: "/home/me/.local/bin/OptionLens.AppImage", isFile: () => true, writable: () => true };

  it("updates only a writable AppImage it runs as", () => {
    expect(linuxLocation(probe)).toEqual({ ok: true, appPath: "/home/me/.local/bin/OptionLens.AppImage" });
    expect(linuxLocation({ ...probe, appImage: undefined })).toEqual({ ok: false, reason: NOT_AN_APPIMAGE });   // extracted / unpacked build
    expect(linuxLocation({ ...probe, appImage: "OptionLens.AppImage" })).toEqual({ ok: false, reason: NOT_AN_APPIMAGE });
    expect(linuxLocation({ ...probe, isFile: () => false })).toEqual({ ok: false, reason: NOT_AN_APPIMAGE });
    expect(linuxLocation({ ...probe, writable: (p) => p !== "/home/me/.local/bin" })).toMatchObject({ ok: false, reason: expect.stringMatching(/no write access/) });
    expect(linuxLocation({ ...probe, packaged: false })).toMatchObject({ ok: false, reason: expect.stringMatching(/development build/) });
  });

  function fakeAppImage(path: string, body = "payload") {
    const head = Buffer.alloc(16);
    Buffer.from([0x7f, 0x45, 0x4c, 0x46]).copy(head, 0);
    Buffer.from("AI\x02", "latin1").copy(head, 8);
    writeFileSync(path, Buffer.concat([head, Buffer.from(body)]), { mode: 0o600 });
  }
  /** `--appimage-extract '*.desktop'` as the AppImage runtime does it: squashfs-root/<name>.desktop in cwd. */
  const extract = (entry: string) => vi.fn(async (_file: string, args: string[], cwd: string) => {
    expect(args).toEqual(["--appimage-extract", "*.desktop"]);
    mkdirSync(join(cwd, "squashfs-root"), { recursive: true });
    writeFileSync(join(cwd, "squashfs-root", "optionlens.desktop"), entry);
    return "";
  });

  it("checks the AppImage's identity and version, then stages it executable next to the running one", async () => {
    const root = tempDir("ol appimage ");
    const bin = join(root, "My Apps");
    mkdirSync(bin);
    mkdirSync(join(root, "stage"));
    const appPath = join(bin, "OptionLens.AppImage");
    fakeAppImage(appPath, "0.1.3");
    writeFileSync(join(bin, stagedName("0.1.2")), "stale");
    const download = join(root, "OptionLens-0.1.4-linux-x64.AppImage");
    fakeAppImage(download, "0.1.4");
    const execIn = extract("[Desktop Entry]\nName=OptionLens\nExec=AppRun --no-sandbox %U\nX-AppImage-Version=0.1.4\n");
    const swap = vi.fn(async () => undefined);
    const i = linuxInstaller({ ...base(root), ...probe, appImage: appPath, execIn, swap });
    const staged = await i.stage(download, join(root, "stage"), "0.1.4", appPath);
    expect(staged).toBe(join(bin, ".OptionLens-0.1.4.AppImage.update"));
    expect(statSync(staged).mode & 0o777).toBe(0o755);
    expect(readFileSync(staged, "latin1").endsWith("0.1.4")).toBe(true);
    expect(readdirSync(bin).sort()).toEqual([".OptionLens-0.1.4.AppImage.update", "OptionLens.AppImage"]);   // the stale one is gone
    expect(existsSync(join(root, "stage", "squashfs-root"))).toBe(false);
    expect(await i.install(staged, appPath, "0.1.4")).toBe(join(root, "backups", "OptionLens-0.1.3.AppImage"));
    expect(swap).toHaveBeenLastCalledWith(expect.objectContaining({ target: appPath, source: staged, open: [appPath], detach: true,
      prune: { dir: join(root, "backups"), keep: 2, suffix: ".AppImage" } }));
  });

  it("refuses something that is not an AppImage, another app, or another version", async () => {
    const root = tempDir();
    mkdirSync(join(root, "stage"));
    const appPath = join(root, "OptionLens.AppImage");
    fakeAppImage(appPath);
    const notAppImage = join(root, "x.AppImage");
    writeFileSync(notAppImage, "#!/bin/sh\necho hi\n");
    const mk = (entry: string) => linuxInstaller({ ...base(root), ...probe, appImage: appPath, execIn: extract(entry), swap: async () => undefined });
    await expect(mk("").stage(notAppImage, join(root, "stage"), "0.1.4", appPath)).rejects.toThrow(/not an AppImage/);
    const download = join(root, "d.AppImage");
    fakeAppImage(download);
    await expect(mk("[Desktop Entry]\nName=Other\nX-AppImage-Version=0.1.4\n").stage(download, join(root, "stage"), "0.1.4", appPath)).rejects.toThrow(/"Other", not OptionLens/);
    await expect(mk("[Desktop Entry]\nName=OptionLens\nX-AppImage-Version=0.1.3\n").stage(download, join(root, "stage"), "0.1.4", appPath)).rejects.toThrow(/version 0.1.3, not 0.1.4/);
    expect(readdirSync(root).filter((n) => n.endsWith(".update"))).toEqual([]);
  });

  // CI (Linux): OPTIONLENS_APPIMAGE=release/OptionLens-<v>-linux-x64.AppImage npx vitest run …/installers.test.ts
  it.runIf(!!process.env.OPTIONLENS_APPIMAGE)("accepts the built AppImage (its own --appimage-extract, real desktop entry)", async () => {
    const version = JSON.parse(readFileSync("package.json", "utf8")).version as string;
    const run = promisify(execFile);
    const execIn = async (file: string, args: string[], cwd: string) => (await run(file, args, { cwd })).stdout;
    await checkAppImage(execIn, resolve(process.env.OPTIONLENS_APPIMAGE!), tempDir(), version);
    await expect(checkAppImage(execIn, resolve(process.env.OPTIONLENS_APPIMAGE!), tempDir(), "9.9.9")).rejects.toThrow(/not 9.9.9/);
  });

  it("starts helpers without the running AppImage's variables", () => {
    expect(withoutAppImageEnv({ HOME: "/h", APPIMAGE: "/a", APPDIR: "/tmp/.mount_x", ARGV0: "x", OWD: "/w" })).toEqual({ HOME: "/h" });
    // AppRun's prefixes go; the user's own entries stay
    expect(withoutAppImageEnv({ APPDIR: "/tmp/.mount_x", PATH: "/tmp/.mount_x:/tmp/.mount_x/usr/sbin:/usr/bin:/tmp/.mount_xyz",
      LD_LIBRARY_PATH: "/tmp/.mount_x/usr/lib", XDG_DATA_DIRS: "/tmp/.mount_x/usr/share/::/usr/share/gnome:/usr/share/" }))
      .toEqual({ PATH: "/usr/bin:/tmp/.mount_xyz", XDG_DATA_DIRS: "/usr/share/gnome:/usr/share/" });
    expect(withoutAppImageEnv({ PATH: "/usr/bin" })).toEqual({ PATH: "/usr/bin" });   // not an AppImage: unchanged
  });
});

describe("Windows installer", () => {
  const exe = "C:\\Users\\Jane Doe\\AppData\\Local\\Programs\\OptionLens\\OptionLens.exe";
  const probe = { packaged: true, execPath: exe, isFile: () => true, writable: () => true };
  const setup = (strings: Record<string, string>) => peFile(versionResource(strings, [0, 1, 4, 0]), Buffer.alloc(4096, 1));

  it("updates only an installer-made per-user installation", () => {
    expect(windowsLocation(probe)).toEqual({ ok: true, appPath: exe });
    expect(windowsLocation({ ...probe, isFile: (p) => !p.endsWith(UNINSTALLER) })).toEqual({ ok: false, reason: NOT_INSTALLED });   // portable zip
    expect(windowsLocation({ ...probe, writable: () => false })).toMatchObject({ ok: false, reason: expect.stringMatching(/cannot write/) });
    expect(windowsLocation({ ...probe, packaged: false })).toMatchObject({ ok: false, reason: expect.stringMatching(/development build/) });
  });

  it("checks the installer's ProductName and ProductVersion", () => {
    expect(setupMismatch(setup({ ProductName: "OptionLens", ProductVersion: "0.1.4" }), "0.1.4")).toBeNull();
    expect(setupMismatch(setup({ ProductName: "Other", ProductVersion: "0.1.4" }), "0.1.4")).toMatch(/"Other", not OptionLens/);
    expect(setupMismatch(setup({ ProductName: "OptionLens", ProductVersion: "0.1.3" }), "0.1.4")).toMatch(/version 0.1.3, not 0.1.4/);
    expect(setupMismatch(Buffer.from("MZ not really"), "0.1.4")).toMatch(/not a Windows installer/);
  });

  it("stages the verified setup.exe, keeps it and the current version's installer, and runs the helper with a fallback", async () => {
    const root = tempDir();
    const backups = join(root, "backups");
    mkdirSync(backups);
    for (const v of ["0.1.1", "0.1.2", "0.1.3"]) writeFileSync(join(backups, `OptionLens-${v}-win-x64-setup.exe`), v);
    writeFileSync(join(backups, "db-0.1.2-20261120T000000Z.duckdb"), "db");
    const download = join(root, "OptionLens-0.1.4-win-x64-setup.exe");
    writeFileSync(download, setup({ ProductName: "OptionLens", ProductVersion: "0.1.4" }));
    const launchHelper = vi.fn(async () => undefined);
    const i = windowsInstaller({ ...base(root), ...probe, isFile: (p) => existsSync(p), launchHelper });
    expect(await i.stage(download, join(root, "stage"), "0.1.4", exe)).toBe(download);
    await expect(i.stage(download, join(root, "stage"), "0.1.5", exe)).rejects.toThrow(/version 0.1.4, not 0.1.5/);
    const backup = await i.install(download, exe, "0.1.4");
    expect(backup).toBe(join(backups, "OptionLens-0.1.3-win-x64-setup.exe"));
    expect(readdirSync(backups).sort()).toEqual(["OptionLens-0.1.3-win-x64-setup.exe", "OptionLens-0.1.4-win-x64-setup.exe", "db-0.1.2-20261120T000000Z.duckdb"]);
    expect(launchHelper).toHaveBeenLastCalledWith({ pid: 4242, installer: download, fallback: backup, app: exe, logFile: join(root, "updater.log") });
    await i.restore(backup, exe);
    expect(launchHelper).toHaveBeenLastCalledWith({ pid: 4242, installer: backup, fallback: null, app: exe, logFile: join(root, "updater.log") });
    expect(i.hasBackup(backup)).toBe(true);
  });

  it("installs without a fallback when the current version's installer was never kept", async () => {
    const root = tempDir();
    const download = join(root, "OptionLens-0.1.4-win-x64-setup.exe");
    writeFileSync(download, "x");
    const launchHelper = vi.fn(async () => undefined);
    const i = windowsInstaller({ ...base(root), ...probe, isFile: (p) => existsSync(p), launchHelper });
    const backup = await i.install(download, exe, "0.1.4");
    expect(launchHelper).toHaveBeenLastCalledWith(expect.objectContaining({ fallback: null }));
    expect(i.hasBackup(backup)).toBe(false);   // a rollback then tells the user instead
  });
});
