import { describe, expect, it } from "vitest";
import { launchAtLoginFromOs, loginItemStatus } from "./login-item";

describe("login item status", () => {
  it("reports what macOS has, including approval pending and items disabled in System Settings", () => {
    expect(loginItemStatus("darwin", { openAtLogin: true, status: "enabled" })).toBe("enabled");
    expect(loginItemStatus("darwin", { openAtLogin: true, status: "requires-approval" })).toBe("requires-approval");
    expect(loginItemStatus("darwin", { openAtLogin: false, status: "not-registered" })).toBe("disabled");
    expect(loginItemStatus("darwin", { openAtLogin: false, status: "not-found" })).toBe("disabled");
    expect(loginItemStatus("darwin", { openAtLogin: true })).toBe("enabled");      // macOS 12
    expect(loginItemStatus("linux", { openAtLogin: true })).toBe("unsupported");
  });

  it("derives the setting from the OS state (a user who removed the item in System Settings is respected)", () => {
    expect(launchAtLoginFromOs("disabled")).toBe(false);
    expect(launchAtLoginFromOs("requires-approval")).toBe(true);
    expect(launchAtLoginFromOs("enabled")).toBe(true);
    expect(launchAtLoginFromOs("unsupported")).toBeNull();
  });
});
