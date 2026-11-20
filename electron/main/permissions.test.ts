import { describe, expect, it } from "vitest";
import { allowPermission } from "./permissions";

describe("allowPermission", () => {
  it("grants Local Network Access (loopback engine) to the app's own origin only", () => {
    for (const p of ["local-network-access", "loopback-network", "local-network"]) {
      expect(allowPermission(p, "app://optionlens")).toBe(true);
      expect(allowPermission(p, "app://optionlens/s/AAPL/chain")).toBe(true);
      expect(allowPermission(p, "https://evil.example")).toBe(false);
      expect(allowPermission(p, "app://other")).toBe(false);
      expect(allowPermission(p, "")).toBe(false);
    }
  });

  it("denies everything else", () => {
    for (const p of ["notifications", "media", "geolocation", "clipboard-read", "openExternal"]) expect(allowPermission(p, "app://optionlens")).toBe(false);
  });
});
