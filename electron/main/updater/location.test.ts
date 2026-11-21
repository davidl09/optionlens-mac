import { describe, expect, it } from "vitest";
import { bundlePathFromExecutable, installLocation, isTranslocated, MOVE_TO_APPLICATIONS } from "./location";

const EXE = "/Applications/OptionLens.app/Contents/MacOS/OptionLens";
const probe = (over: Partial<Parameters<typeof installLocation>[0]> = {}) => installLocation({ platform: "darwin", packaged: true, execPath: EXE, writable: () => true, ...over });

describe("install location", () => {
  it("finds the bundle from the executable", () => {
    expect(bundlePathFromExecutable(EXE)).toBe("/Applications/OptionLens.app");
    expect(bundlePathFromExecutable("/Users/me/Apps folder/OptionLens.app/Contents/MacOS/OptionLens")).toBe("/Users/me/Apps folder/OptionLens.app");
    expect(bundlePathFromExecutable("/usr/bin/electron")).toBeNull();
    expect(bundlePathFromExecutable("/x/OptionLens/Contents/MacOS/OptionLens")).toBeNull();
  });

  it("installs into a writable, non-translocated bundle", () => {
    expect(probe()).toEqual({ ok: true, bundlePath: "/Applications/OptionLens.app" });
    expect(probe({ execPath: "/Users/me/Applications/OptionLens.app/Contents/MacOS/OptionLens" })).toMatchObject({ ok: true });
  });

  it("asks to move the app when it is translocated, on a read-only volume or not writable", () => {
    const translocated = "/private/var/folders/xy/T/AppTranslocation/1234-ABCD/d/OptionLens.app/Contents/MacOS/OptionLens";
    expect(isTranslocated("/private/var/folders/xy/T/AppTranslocation/1234-ABCD/d/OptionLens.app")).toBe(true);
    expect(probe({ execPath: translocated })).toEqual({ ok: false, reason: MOVE_TO_APPLICATIONS });
    expect(probe({ execPath: "/Volumes/OptionLens 0.1.2/OptionLens.app/Contents/MacOS/OptionLens" })).toEqual({ ok: false, reason: MOVE_TO_APPLICATIONS });
    expect(probe({ writable: (d) => d !== "/Applications" })).toEqual({ ok: false, reason: MOVE_TO_APPLICATIONS });
    expect(probe({ writable: (d) => d !== "/Applications/OptionLens.app" })).toEqual({ ok: false, reason: MOVE_TO_APPLICATIONS });
  });

  it("does not install from development builds or other platforms", () => {
    expect(probe({ packaged: false })).toMatchObject({ ok: false, reason: expect.stringMatching(/packaged/) });
    expect(probe({ platform: "linux" })).toMatchObject({ ok: false, reason: expect.stringMatching(/macOS/) });
  });
});
