import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { peFile, versionResource } from "./pe-fixtures.test-helper";
import { peImageEnd, readPeVersion } from "./pe-version";

describe("PE version resource", () => {
  const strings = { CompanyName: "OptionLens", ProductName: "OptionLens", ProductVersion: "0.1.4", FileVersion: "0.1.4" };

  it("reads ProductName, ProductVersion and the fixed version from the image", () => {
    const v = readPeVersion(peFile(Buffer.concat([Buffer.alloc(36, 0xab), versionResource(strings, [0, 1, 4, 0])])));
    expect(v).toEqual({ fixedProductVersion: "0.1.4.0", strings });
  });

  it("ignores a version resource in the appended payload and anything that is not a PE file", () => {
    const fake = versionResource({ ProductName: "OptionLens", ProductVersion: "9.9.9" }, [9, 9, 9, 0]);
    const exe = peFile(Buffer.alloc(64), Buffer.concat([Buffer.alloc(4), fake]));
    expect(peImageEnd(exe)).toBe(0x200 + 64);
    expect(readPeVersion(exe)).toBeNull();
    expect(readPeVersion(Buffer.from("#!/bin/sh\necho not a PE file\n".padEnd(200, " ")))).toBeNull();
    expect(readPeVersion(Buffer.alloc(10))).toBeNull();
    const truncated = peFile(versionResource(strings, [0, 1, 4, 0]));
    expect(readPeVersion(truncated.subarray(0, truncated.length - 40))).toBeNull();
  });

  // CI (Windows): OPTIONLENS_SETUP_EXE=release/OptionLens-<v>-win-x64-setup.exe npx vitest run …/pe-version.test.ts
  it.runIf(!!process.env.OPTIONLENS_SETUP_EXE)("reads the version of the built installer", () => {
    const v = readPeVersion(readFileSync(process.env.OPTIONLENS_SETUP_EXE!));
    const version = JSON.parse(readFileSync("package.json", "utf8")).version as string;
    expect(v?.strings).toMatchObject({ ProductName: "OptionLens", ProductVersion: version });
    expect(v?.fixedProductVersion).toBe(`${version}.0`);
  });
});
