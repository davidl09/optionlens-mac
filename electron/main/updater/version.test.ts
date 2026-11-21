import { describe, expect, it } from "vitest";
import { compareVersions, isUpdate, parseVersion } from "./version";

describe("versions", () => {
  it("parses x.y.z with an optional v, prerelease and build", () => {
    expect(parseVersion("v0.1.3")).toEqual({ major: 0, minor: 1, patch: 3, pre: [] });
    expect(parseVersion("1.2.3-beta.2+sha.1")).toEqual({ major: 1, minor: 2, patch: 3, pre: ["beta", "2"] });
    for (const bad of ["", "1.2", "1.2.3.4", "01.2.3", "1.2.x", "latest", "1.2.3-", "v1.2.3 extra"]) expect(parseVersion(bad)).toBeNull();
  });

  it("orders numerically, prereleases before their release", () => {
    const order = ["0.1.2", "0.1.3-alpha", "0.1.3-alpha.1", "0.1.3-alpha.beta", "0.1.3-beta.2", "0.1.3-beta.11", "0.1.3", "0.1.10", "0.2.0", "1.0.0"];
    for (let i = 1; i < order.length; i++) {
      expect(compareVersions(parseVersion(order[i - 1])!, parseVersion(order[i])!)).toBe(-1);
      expect(compareVersions(parseVersion(order[i])!, parseVersion(order[i - 1])!)).toBe(1);
    }
    expect(compareVersions(parseVersion("v1.0.0")!, parseVersion("1.0.0+build")!)).toBe(0);
  });

  it("an update is a strictly newer release: never a downgrade, the same version or a prerelease", () => {
    expect(isUpdate("0.1.3", "0.1.2")).toBe(true);
    expect(isUpdate("v0.1.10", "0.1.9")).toBe(true);
    expect(isUpdate("0.1.2", "0.1.2")).toBe(false);
    expect(isUpdate("0.1.1", "0.1.2")).toBe(false);
    expect(isUpdate("0.2.0-beta.1", "0.1.2")).toBe(false);
    expect(isUpdate("0.1.3", "0.1.3-rc.1")).toBe(true);
    expect(isUpdate("garbage", "0.1.2")).toBe(false);
    expect(isUpdate("0.1.3", "garbage")).toBe(false);
  });
});
