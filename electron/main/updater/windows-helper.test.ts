import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, win32 } from "node:path";
import { describe, expect, it } from "vitest";
import { launchWindowsHelper, POWERSHELL_ARGS, psQuote, windowsHelperScript, type WindowsHelperPlan } from "./windows-helper";

const plan: WindowsHelperPlan = {
  pid: 4242,
  installer: "C:\\Users\\Zoë O'Brien\\AppData\\Local\\Temp\\optionlens-update-1\\OptionLens-0.1.4-win-x64-setup.exe",
  fallback: "C:\\Users\\Zoë O'Brien\\AppData\\Roaming\\OptionLens\\backups\\OptionLens-0.1.3-win-x64-setup.exe",
  app: "C:\\Users\\Zoë O'Brien\\AppData\\Local\\Programs\\OptionLens\\OptionLens.exe",
  logFile: "C:\\Users\\Zoë O'Brien\\AppData\\Roaming\\OptionLens\\logs\\updater.log",
  workDir: "C:\\Users\\Zoë O'Brien\\AppData\\Local\\Temp\\optionlens-update-1",
};

describe("Windows update helper", () => {
  it("quotes for PowerShell, including typographic single quotes", () => {
    expect(psQuote("C:\\a b\\it's")).toBe("'C:\\a b\\it''s'");
    expect(psQuote("C:\\it’s $HOME `x` \"y\"")).toBe("'C:\\it’’s $HOME `x` \"y\"'");
  });

  it("contains only quoted inputs and runs the installer silently with the update flag", () => {
    const s = windowsHelperScript(plan);
    expect(s).toContain(`$Installer = ${psQuote(plan.installer)}\r\n`);
    expect(s).toContain(`$App = ${psQuote(plan.app)}\r\n`);
    expect(s).toContain("$AppPid = 4242\r\n");
    expect(s).toContain("-ArgumentList '/S', '--updated'");
    expect(s).not.toMatch(/[^\r]\n/);                       // CRLF throughout
    expect(windowsHelperScript({ ...plan, fallback: null })).toContain("$Fallback = ''\r\n");
  });

  it("rejects relative, POSIX and control-character paths and bad pids", () => {
    expect(() => windowsHelperScript({ ...plan, app: "OptionLens.exe" })).toThrow(/absolute Windows path/);
    expect(() => windowsHelperScript({ ...plan, app: "/Applications/OptionLens.app" })).toThrow(/absolute Windows path/);
    expect(() => windowsHelperScript({ ...plan, installer: "C:\\a\r\nRemove-Item C:\\" })).toThrow(/control/);
    expect(() => windowsHelperScript({ ...plan, pid: 0 })).toThrow(/pid/);
    expect(windowsHelperScript({ ...plan, logFile: "\\\\server\\share\\updater.log" })).toContain("$Log = '\\\\server\\share\\updater.log'");
  });

  it("runs the installer from a copy in its own temp folder, writes the script with a BOM and starts PowerShell detached", async () => {
    const calls: string[][] = [];
    let written = "";
    const work = "C:\\Temp\\optionlens-update-7";
    await launchWindowsHelper({ pid: 4242, installer: "C:\\Users\\me\\AppData\\Roaming\\OptionLens\\updates\\0.1.4\\stage\\OptionLens-0.1.4-win-x64-setup.exe",
      fallback: null, app: plan.app, logFile: plan.logFile }, {
      makeTempDir: async () => work,
      copyFile: async (a, b) => { calls.push(["copy", a, b]); },
      writeFile: async (p, c) => { calls.push(["write", p]); written = c; },
      spawnDetached: (f, a) => { calls.push([f, ...a]); },
      powershell: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
    });
    const copy = win32.join(work, "OptionLens-0.1.4-win-x64-setup.exe");
    expect(calls[0]).toEqual(["copy", expect.stringContaining("stage"), copy]);
    expect(calls[1]).toEqual(["write", win32.join(work, "update.ps1")]);
    expect(calls[2]).toEqual(["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", ...POWERSHELL_ARGS, win32.join(work, "update.ps1")]);
    expect(written.startsWith("\ufeff")).toBe(true);
    expect(written).toContain(`$Installer = ${psQuote(copy)}`);
  });

  // Runs the generated script with Windows PowerShell 5.1 (CI runs this file on windows-latest).
  // Windows PowerShell 5.1 takes seconds to start on a CI runner (one start per test, plus the installer .bat files and the
  // detached relaunch): these tests get a timeout sized for that, not vitest's 5 s default.
  const LAUNCH_WAIT_MS = 10_000;
  const WINDOWS_TEST_MS = 60_000;
  describe.runIf(process.platform === "win32")("on Windows", () => {
    const deadPid = () => Number(spawnSync("cmd.exe", ["/c", "exit"]).pid);
    function run(o: { installerExit: number; fallback: boolean }) {
      const root = mkdtempSync(join(tmpdir(), "ol helper ' ’"));
      const work = join(root, "work dir");
      mkdirSync(work);
      // The fake installers are .bat files, which cmd reads in the OEM code page: their marker file has an
      // ASCII path. The helper's own paths (script, installer, app, log) keep the space, ' and ’.
      const marksDir = mkdtempSync(join(tmpdir(), "ol-marks-"));
      const marks = join(marksDir, "marks.txt");
      const bat = (name: string, body: string) => { const f = join(root, name); writeFileSync(f, `@echo off\r\n${body}\r\n`); return f; };
      const installer = bat("setup new.bat", `echo installed %*>>"${marks}"\r\nexit /b ${o.installerExit}`);
      const fallback = o.fallback ? bat("setup old.bat", `echo restored %*>>"${marks}"\r\nexit /b 0`) : null;
      const app = bat("OptionLens.bat", `echo launched>>"${marks}"`);
      const script = join(work, "update.ps1");
      const logFile = join(root, "updater.log");
      writeFileSync(script, `\ufeff${windowsHelperScript({ pid: deadPid(), installer, fallback, app, logFile, workDir: work })}`);
      const r = spawnSync(join(process.env.SystemRoot ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe"), [...POWERSHELL_ARGS, script], { encoding: "utf8" });
      const read = (f: string) => (existsSync(f) ? readFileSync(f, "utf8").replace(/\r/g, "") : "");
      // The relaunch is detached: wait for its mark up to a deadline, sleeping in-process (spawning a PowerShell per
      // poll, as before, cost about a second each on CI runners and pushed the test past its timeout).
      const tick = new Int32Array(new SharedArrayBuffer(4));
      for (const until = Date.now() + LAUNCH_WAIT_MS; Date.now() < until && !read(marks).includes("launched");) Atomics.wait(tick, 0, 0, 100);
      const out = { status: r.status, marks: read(marks), log: `${read(logFile)}${r.stdout}${r.stderr}`, workGone: !existsSync(work) };
      rmSync(marksDir, { recursive: true, force: true });
      rmSync(root, { recursive: true, force: true });
      return out;
    }
    it("installs silently, relaunches the app and cleans up", () => {
      const r = run({ installerExit: 0, fallback: true });
      expect(r.status).toBe(0);
      expect(r.marks, r.log).toBe("installed /S --updated\nlaunched\n");
      expect(r.log).toMatch(/installed .*setup new.bat/);
      expect(r.workGone).toBe(true);
    }, WINDOWS_TEST_MS);
    it("runs the previous version's installer when the new one fails", () => {
      const r = run({ installerExit: 2, fallback: true });
      expect(r.status).toBe(1);
      expect(r.marks, r.log).toBe("installed /S --updated\nrestored /S --updated\nlaunched\n");
      expect(r.log).toMatch(/failed \(exit code 2\)/);
    }, WINDOWS_TEST_MS);
  });
});
