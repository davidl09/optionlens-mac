import { describe, expect, it } from "vitest";
import type { UpdateStatus } from "../shared/api";
import { updateMenuItem } from "./status-text";

const u = (phase: UpdateStatus["phase"], progress: number | null = null): UpdateStatus => ({
  phase, currentVersion: "0.1.3", progress, error: null, blocked: null, skipped: false, notice: null, checkedAt: null,
  latest: { version: "0.1.4", notes: "", url: "", publishedAt: null },
});

describe("menu-bar update item", () => {
  it("offers the update while one exists, and Restart now once it is ready", () => {
    expect(updateMenuItem(u("available"))).toEqual({ label: "Update available (0.1.4)…", action: "settings" });
    expect(updateMenuItem(u("downloading", 0.256))).toEqual({ label: "Downloading update (0.1.4)… 26%", action: "settings" });
    expect(updateMenuItem(u("ready"))).toEqual({ label: "Restart now to update (0.1.4)", action: "install" });
    for (const p of ["idle", "checking", "up-to-date", "installing", "error"] as const) expect(updateMenuItem(u(p))).toBeNull();
    expect(updateMenuItem({ ...u("available"), latest: null })).toBeNull();
  });
});
