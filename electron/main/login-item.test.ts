import { describe, expect, it } from "vitest";
import { launchAtLoginFromOs } from "./login-item";

describe("login item", () => {
  it("derives the setting from the OS state (a user who removed the item outside OptionLens is respected)", () => {
    expect(launchAtLoginFromOs("disabled")).toBe(false);
    expect(launchAtLoginFromOs("requires-approval")).toBe(true);
    expect(launchAtLoginFromOs("enabled")).toBe(true);
    expect(launchAtLoginFromOs("unsupported")).toBeNull();
  });
});
