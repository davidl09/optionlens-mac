import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { bundleMismatch, stageBundle, type Exec } from "./stage";

/** Fake ditto / PlistBuddy / codesign: ditto lays out `entries` in the staging folder. */
function fakeExec(o: { entries?: string[]; link?: boolean; plist?: Record<string, string>; codesignFails?: boolean }) {
  const calls: string[][] = [];
  const exec: Exec = async (file, args) => {
    calls.push([file, ...args]);
    if (file === "/usr/bin/ditto") {
      const out = args[3];
      for (const e of o.entries ?? ["OptionLens.app"]) {
        if (o.link) symlinkSync("/Applications", join(out, e));
        else mkdirSync(join(out, e, "Contents"), { recursive: true });
      }
      return "";
    }
    if (file === "/usr/libexec/PlistBuddy") return `${(o.plist ?? { CFBundleIdentifier: "app.optionlens.mac", CFBundleShortVersionString: "0.1.3" })[args[1].replace("Print :", "")] ?? ""}\n`;
    if (file === "/usr/bin/codesign") { if (o.codesignFails) throw new Error("a sealed resource is missing or invalid"); return ""; }
    throw new Error(`unexpected ${file}`);
  };
  return { exec, calls };
}

describe("staging", () => {
  let dir: string;
  beforeEach(() => { dir = mkdtempSync(join(tmpdir(), "ol-stage-")); writeFileSync(join(dir, "u.zip"), "zip"); });
  afterEach(() => { rmSync(dir, { recursive: true, force: true }); });

  it("extracts with ditto into an empty folder, checks the plist and the signature", async () => {
    const stage = join(dir, "stage");
    mkdirSync(stage);
    writeFileSync(join(stage, "leftover"), "");
    const f = fakeExec({});
    const app = await stageBundle(f.exec, join(dir, "u.zip"), stage, "0.1.3");
    expect(app).toBe(join(stage, "OptionLens.app"));
    expect(f.calls[0]).toEqual(["/usr/bin/ditto", "-x", "-k", join(dir, "u.zip"), stage]);
    expect(f.calls.at(-1)).toEqual(["/usr/bin/codesign", "--verify", "--deep", "--strict", app]);
  });

  it.each([
    [{ entries: ["OptionLens.app", "__MACOSX"] }, /only OptionLens.app/],
    [{ entries: ["Other.app"] }, /only OptionLens.app/],
    [{ link: true }, /only OptionLens.app/],
    [{ plist: { CFBundleIdentifier: "com.evil.app", CFBundleShortVersionString: "0.1.3" } }, /bundle id/],
    [{ plist: { CFBundleIdentifier: "app.optionlens.mac", CFBundleShortVersionString: "0.1.2" } }, /version 0.1.2, not 0.1.3/],
    [{ codesignFails: true }, /signature is invalid/],
  ])("refuses %j", async (o, msg) => {
    await expect(stageBundle(fakeExec(o).exec, join(dir, "u.zip"), join(dir, "stage"), "0.1.3")).rejects.toThrow(msg);
  });

  it("bundleMismatch", () => {
    expect(bundleMismatch({ bundleId: "app.optionlens.mac", version: "1.0.0" }, "1.0.0")).toBeNull();
    expect(bundleMismatch({ bundleId: "app.optionlens.mac", version: "" }, "1.0.0")).toMatch(/\(none\)/);
  });
});
