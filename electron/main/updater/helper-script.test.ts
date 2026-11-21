import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { helperScript, shQuote, type SwapPlan } from "./helper-script";
import { launchSwap } from "./swap";

/** A pid that is not running (the helper's wait loop ends at once). */
function deadPid(): number {
  const r = spawnSync("/bin/sh", ["-c", "echo $$"]);
  return Number(String(r.stdout).trim());
}

function makeApp(path: string, version: string) {
  mkdirSync(join(path, "Contents"), { recursive: true });
  writeFileSync(join(path, "Contents", "version"), version);
}
const version = (app: string) => readFileSync(join(app, "Contents", "version"), "utf8");

describe("helper script", () => {
  let root: string;
  let plan: SwapPlan;
  beforeEach(() => {
    // Spaces and a quote in every path, like "Application Support".
    root = mkdtempSync(join(tmpdir(), "ol helper '"));
    const support = join(root, "Library", "Application Support", "OptionLens");
    mkdirSync(join(root, "Applications"), { recursive: true });
    mkdirSync(join(root, "work dir"), { recursive: true });
    makeApp(join(root, "Applications", "OptionLens.app"), "0.1.2");
    makeApp(join(support, "updates", "0.1.3", "stage", "OptionLens.app"), "0.1.3");
    const opener = join(root, "fake open.sh");
    writeFileSync(opener, `#!/bin/bash\nprintf '%s\\n' "$1" >> ${shQuote(join(root, "opened.txt"))}\n`, { mode: 0o755 });
    plan = {
      pid: deadPid(), target: join(root, "Applications", "OptionLens.app"), source: join(support, "updates", "0.1.3", "stage", "OptionLens.app"),
      archive: join(support, "backups", "OptionLens-0.1.2.app"), prune: { dir: join(support, "backups"), keep: 2 },
      logFile: join(root, "updater.log"), workDir: join(root, "work dir"), open: opener,
    };
  });
  afterEach(() => { rmSync(root, { recursive: true, force: true }); });
  const run = (p: SwapPlan) => { const f = join(root, "swap.sh"); writeFileSync(f, helperScript(p)); return spawnSync("/bin/bash", [f], { encoding: "utf8" }); };

  it("quotes for the shell", () => {
    expect(shQuote("/a b/it's")).toBe(`'/a b/it'\\''s'`);
    expect(execFileSync("/bin/bash", ["-c", `printf %s ${shQuote("a 'b' $HOME `x` \\n")}`], { encoding: "utf8" })).toBe("a 'b' $HOME `x` \\n");
  });

  it("is valid bash and contains only quoted inputs", () => {
    const s = helperScript(plan);
    expect(spawnSync("/bin/bash", ["-n", "-c", s]).status).toBe(0);
    expect(s).toContain(`TARGET=${shQuote(plan.target)}`);
    expect(s).toContain(`ARCHIVE=${shQuote(plan.archive)}`);
  });

  it("rejects relative paths, control characters and bad pids", () => {
    expect(() => helperScript({ ...plan, target: "Applications/OptionLens.app" })).toThrow(/absolute/);
    expect(() => helperScript({ ...plan, source: "/a\nrm -rf /" })).toThrow(/control/);
    expect(() => helperScript({ ...plan, pid: 1 })).toThrow(/pid/);
    expect(() => helperScript({ ...plan, pid: 1.5 })).toThrow(/pid/);
  });

  it("swaps the bundle, keeps the previous one as a backup, prunes to 2, opens the app and cleans up", () => {
    const backups = plan.prune!.dir;
    mkdirSync(backups, { recursive: true });
    for (const [v, t] of [["0.1.0", 1000], ["0.1.1", 2000]] as const) { makeApp(join(backups, `OptionLens-${v}.app`), v); utimesSync(join(backups, `OptionLens-${v}.app`), t, t); }
    const r = run(plan);
    expect(r.status).toBe(0);
    expect(version(plan.target)).toBe("0.1.3");
    expect(readdirSync(backups).sort()).toEqual(["OptionLens-0.1.1.app", "OptionLens-0.1.2.app"]);
    expect(version(join(backups, "OptionLens-0.1.2.app"))).toBe("0.1.2");
    expect(readFileSync(join(root, "opened.txt"), "utf8")).toBe(`${plan.target}\n`);
    expect(existsSync(plan.workDir)).toBe(false);
    expect(readFileSync(plan.logFile, "utf8")).toMatch(/replaced .*OptionLens.app/);
  });

  it("puts the previous app back and opens it when the new one cannot be moved in", () => {
    rmSync(plan.source, { recursive: true });
    const r = run(plan);
    expect(r.status).toBe(1);
    expect(version(plan.target)).toBe("0.1.2");
    expect(readFileSync(join(root, "opened.txt"), "utf8")).toBe(`${plan.target}\n`);
    expect(readFileSync(plan.logFile, "utf8")).toMatch(/missing; nothing changed/);
  });
});

describe("launchSwap (rollback)", () => {
  it("discards the bad bundle inside the helper's temp folder and restores the backup", async () => {
    const root = mkdtempSync(join(tmpdir(), "ol rollback "));
    try {
      const target = join(root, "Applications", "OptionLens.app");
      const backup = join(root, "backups", "OptionLens-0.1.2.app");
      makeApp(target, "0.1.3");
      makeApp(backup, "0.1.2");
      const work = join(root, "tmp work");
      let spawned: string[] = [];
      await launchSwap({ pid: deadPid(), target, source: backup, archive: null, prune: null, logFile: join(root, "log"), open: "/bin/true" }, {
        makeTempDir: async () => { mkdirSync(work); return work; },
        writeFile: async (p, c, mode) => writeFileSync(p, c, { mode }),
        spawnDetached: (file, args) => { spawned = [file, ...args]; },
      });
      expect(spawned).toEqual(["/bin/bash", join(work, "swap.sh")]);
      expect(spawnSync(spawned[0], spawned.slice(1)).status).toBe(0);
      expect(version(target)).toBe("0.1.2");
      expect(existsSync(backup)).toBe(false);
      expect(existsSync(work)).toBe(false);
    } finally { rmSync(root, { recursive: true, force: true }); }
  });
});
