import { describe, expect, it } from "vitest";
import { inCloseCollectionWindow, installBlocker, type InstallContext } from "./safe-window";

const at = (iso: string) => new Date(iso);
const ctx = (over: Partial<InstallContext> = {}): InstallContext => ({ now: at("2026-11-18T15:00:00Z"), importing: false, idleSeconds: 600, windowFocused: false, ...over });

describe("safe-window policy", () => {
  it("16:15–16:50 New York time on weekdays, in winter (EST, UTC-5) and summer (EDT, UTC-4)", () => {
    expect(inCloseCollectionWindow(at("2026-01-14T21:15:00Z"))).toBe(true);    // Wed 16:15 EST
    expect(inCloseCollectionWindow(at("2026-01-14T21:49:59Z"))).toBe(true);    // 16:49:59
    expect(inCloseCollectionWindow(at("2026-01-14T21:50:00Z"))).toBe(false);   // 16:50
    expect(inCloseCollectionWindow(at("2026-01-14T21:14:59Z"))).toBe(false);   // 16:14
    expect(inCloseCollectionWindow(at("2026-07-15T20:20:00Z"))).toBe(true);    // Wed 16:20 EDT
    expect(inCloseCollectionWindow(at("2026-07-15T21:20:00Z"))).toBe(false);   // 17:20 EDT (16:20 would be EST)
    expect(inCloseCollectionWindow(at("2026-03-09T20:30:00Z"))).toBe(true);    // Mon after the DST switch (Mar 8): 16:30 EDT
    expect(inCloseCollectionWindow(at("2026-11-02T21:30:00Z"))).toBe(true);    // Mon after DST ends (Nov 1): 16:30 EST
    expect(inCloseCollectionWindow(at("2026-11-21T21:30:00Z"))).toBe(false);   // Saturday
    expect(inCloseCollectionWindow(at("2026-11-22T21:30:00Z"))).toBe(false);   // Sunday
    expect(inCloseCollectionWindow(at("2026-11-20T02:30:00Z"))).toBe(false);   // Thu 21:30 ET (Friday in UTC)
  });

  it("automatic installs need an idle Mac and no focused window; never during an import or the close", () => {
    expect(installBlocker(ctx(), false)).toBeNull();
    expect(installBlocker(ctx({ idleSeconds: 299 }), false)).toMatch(/idle/);
    expect(installBlocker(ctx({ windowFocused: true }), false)).toMatch(/idle/);
    expect(installBlocker(ctx({ importing: true }), false)).toMatch(/import/);
    expect(installBlocker(ctx({ now: at("2026-11-18T21:20:00Z") }), false)).toMatch(/16:15–16:50/);
  });

  it("Restart now skips the idle rule but not the import or the close", () => {
    expect(installBlocker(ctx({ idleSeconds: 0, windowFocused: true }), true)).toBeNull();
    expect(installBlocker(ctx({ importing: true }), true)).toMatch(/import/);
    expect(installBlocker(ctx({ now: at("2026-11-18T21:20:00Z") }), true)).toMatch(/closing data/);
  });
});
