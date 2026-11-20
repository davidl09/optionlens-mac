import { describe, expect, it } from "vitest";
import { batchLines, DEFAULT_SETTINGS, parseStoredSettings, portLabel, portsToTry, validateSettings } from "./settings";

describe("settings validation", () => {
  const ok = { bridge: { enabled: true, port: null, clientId: 17, marketDataLines: 100 }, launchAtLogin: true };
  it("accepts valid settings", () => {
    expect(validateSettings(ok)).toEqual({ ok: true, value: ok });
    expect(validateSettings({ ...ok, bridge: { ...ok.bridge, port: 4002 } }).ok).toBe(true);
  });
  it.each([
    [{ ...ok, bridge: { ...ok.bridge, port: 0 } }, /Port/],
    [{ ...ok, bridge: { ...ok.bridge, port: 70000 } }, /Port/],
    [{ ...ok, bridge: { ...ok.bridge, clientId: 1.5 } }, /Client ID/],
    [{ ...ok, bridge: { ...ok.bridge, clientId: -1 } }, /Client ID/],
    [{ ...ok, bridge: { ...ok.bridge, marketDataLines: 2 } }, /lines/],
    [{ ...ok, bridge: { ...ok.bridge, enabled: "yes" } }, /Enabled/],
    [{ ...ok, launchAtLogin: 1 }, /login/],
    [null, /object/],
  ])("rejects %j", (input, msg) => {
    const r = validateSettings(input);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toMatch(msg);
  });
  it("parses stored files tolerantly", () => {
    expect(parseStoredSettings(null)).toEqual(DEFAULT_SETTINGS);
    expect(parseStoredSettings({ bridge: { port: 7497 }, launchAtLogin: true })).toEqual({ bridge: { ...DEFAULT_SETTINGS.bridge, port: 7497 }, launchAtLogin: true });
    expect(parseStoredSettings({ bridge: { port: "x" } }).bridge).toEqual(DEFAULT_SETTINGS.bridge);
  });
  it("derives ports and batch sizes", () => {
    expect(portsToTry(DEFAULT_SETTINGS.bridge)).toEqual([4001, 7496, 4002, 7497]);
    expect(portsToTry({ ...DEFAULT_SETTINGS.bridge, port: 4002 })).toEqual([4002]);
    expect(portLabel(4001)).toBe("IB Gateway (live)");
    expect(batchLines({ ...DEFAULT_SETTINGS.bridge, marketDataLines: 100 })).toBe(90);
    expect(batchLines({ ...DEFAULT_SETTINGS.bridge, marketDataLines: 5 })).toBe(1);
  });
});
