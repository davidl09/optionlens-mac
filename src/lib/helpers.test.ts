// Unit tests of the renderer's pure helpers (vitest, node).
import { describe, expect, it } from "vitest";
import type { BridgeStatus } from "../../electron/shared/api";
import type { ColumnDef, SymbolRef } from "../api/types";
import { fundamentalText, shortSector } from "./format";
import { sellSplit } from "./insiders";
import { rankSymbols } from "./search";
import { bridgePillLabel, jobState, nextCollectionWindow, readableDetail } from "./status";
import { templateUsesWidth } from "./strategy";
import { emptyColumnKeys, humanize } from "./table";

const col = (key: string): ColumnDef => ({ key, label: key, fmt: "num" });

describe("emptyColumnKeys", () => {
  it("lists only columns without a value in any row", () => {
    const rows = [{ a: 1, b: null, c: "" }, { a: null, b: undefined, c: "x" }];
    expect([...emptyColumnKeys([col("a"), col("b"), col("c"), col("d")], rows)].sort()).toEqual(["b", "d"]);
  });
  it("hides nothing when there are no rows", () => {
    expect(emptyColumnKeys([col("a")], []).size).toBe(0);
  });
});

describe("humanize", () => {
  it("spells payload keys for people", () => {
    expect(humanize("pc_history")).toBe("P/C history");
    expect(humanize("iv30_chg")).toBe("IV30 chg");
  });
});

describe("rankSymbols", () => {
  const s = (symbol: string, name: string): SymbolRef => ({ symbol, name, kind: "stock" });
  it("ranks exact symbol, then symbol prefix (shorter first), then name matches", () => {
    const items = [s("KOLD", "ProShares UltraShort Bloomberg Natural Gas"), s("KODK", "Eastman Kodak"), s("COKE", "Coca-Cola Consolidated"),
      s("KO", "Coca-Cola Co"), s("KOS", "Kosmos Energy"), s("XYZ", "Ko Holdings")];
    expect(rankSymbols("ko", items).map((x) => x.symbol)).toEqual(["KO", "KOS", "KOLD", "KODK", "XYZ", "COKE"]);
  });
  it("keeps the engine's order without a query", () => {
    const items = [s("B", "b"), s("A", "a")];
    expect(rankSymbols(" ", items)).toEqual(items);
  });
});

describe("fundamentalText", () => {
  it("shows liquidity ratios as ratios and full-dollar market caps compactly", () => {
    expect(fundamentalText("Current Ratio", "102.31998%")).toBe("1.02");
    expect(fundamentalText("Quick Ratio", "1,250.5%")).toBe("12.51");
    expect(fundamentalText("Payout Ratio", "15.2%")).toBe("15.2%");
    expect(fundamentalText("Gross Margin", "46.2%")).toBe("46.2%");
    expect(fundamentalText("Market Cap", "4,978,144,265,920")).toBe("4.98T");
    expect(fundamentalText("Total revenue", "$391,035,000")).toBe("$391,035,000");
  });
});

describe("shortSector", () => {
  it("abbreviates long sector names deliberately", () => {
    expect(shortSector("Consumer Discretionary")).toBe("Cons. Disc.");
    expect(shortSector("Consumer Staples")).toBe("Cons. Staples");
    expect(shortSector("Communication Services")).toBe("Comm. Svcs");
    expect(shortSector("Energy")).toBe("Energy");
  });
});

describe("jobState / readableDetail", () => {
  it("marks a run that archived nothing as a warning", () => {
    expect(jobState({ ok: false, finished_at: "x", detail: "2026-11-20: nothing archived — no closing chain" })).toBe("warning");
    expect(jobState({ ok: true, finished_at: "x", detail: "2026-11-20: 0 option_eod rows, 0 bars" })).toBe("warning");
    expect(jobState({ ok: true, finished_at: "x", detail: "2026-11-20: 1,204 option_eod rows, 3 bars" })).toBe("ok");
    expect(jobState({ ok: false, finished_at: "x", detail: "boom" })).toBe("failed");
    expect(jobState({ ok: null, finished_at: null, detail: null })).toBe("running");
  });
  it("never shows raw JSON", () => {
    expect(readableDetail('{"cursor": 12, "events": 51664, "rate": 0.042300000000000004}')).toBe("cursor 12, events 51,664, rate 0.0423");
    expect(readableDetail("deleted: option_eod 0")).toBe("deleted: option_eod 0");
  });
});

describe("bridgePillLabel", () => {
  const base: BridgeStatus = {
    enabled: true, tws: "connected", port: 4001, endpoint: "IB Gateway (live)", accountType: "live", paused: null, lastError: null,
    notes: [], lastPushAt: null, current: null, queued: {} as BridgeStatus["queued"], backfill: { total: 0, done: 0, failed: 0 },
  };
  it("reads in plain words", () => {
    expect(bridgePillLabel(base)).toBe("IBKR connected");
    expect(bridgePillLabel({ ...base, notes: ["IBKR delayed (no OPRA subscription)"] })).toBe("IBKR connected · delayed data");
    expect(bridgePillLabel({ ...base, tws: "disconnected" as BridgeStatus["tws"] })).toBe("IBKR not connected");
    expect(bridgePillLabel({ ...base, enabled: false })).toBe("IBKR off");
    expect(bridgePillLabel({ ...base, paused: "competing session" })).toBe("IBKR connected · paused");
  });
});

describe("nextCollectionWindow", () => {
  it("is null while the 09:00–20:00 ET weekday window is open", () => {
    expect(nextCollectionWindow(new Date("2026-11-23T15:00:00Z"))).toBeNull();   // Mon 10:00 ET
  });
  it("names the next weekday window", () => {
    expect(nextCollectionWindow(new Date("2026-11-21T17:00:00Z"))).toBe("Mon 09:00 ET");   // Sat
    expect(nextCollectionWindow(new Date("2026-11-23T12:00:00Z"))).toBe("today 09:00 ET");  // Mon 07:00 ET
    expect(nextCollectionWindow(new Date("2026-11-24T02:00:00Z"))).toBe("tomorrow 09:00 ET");  // Mon 21:00 ET
    expect(nextCollectionWindow(new Date("2026-11-28T02:00:00Z"))).toBe("Mon 09:00 ET");  // Fri 21:00 ET
  });
});

describe("templateUsesWidth", () => {
  it("only for templates with two same-type legs at different strikes", () => {
    const o = (right: "C" | "P", strike_offset_pct: number) => ({ kind: "option" as const, right, qty: 1, strike_offset_pct });
    expect(templateUsesWidth({ legs: [o("C", 0)] })).toBe(false);
    expect(templateUsesWidth({ legs: [o("C", 0), o("P", 0)] })).toBe(false);
    expect(templateUsesWidth({ legs: [o("C", 0.05), o("P", -0.05)] })).toBe(false);
    expect(templateUsesWidth({ legs: [o("C", 0), o("C", 0.05)] })).toBe(true);
    expect(templateUsesWidth({ legs: [o("P", -0.05), o("P", 0), o("C", 0), o("C", 0.05)] })).toBe(true);
    expect(templateUsesWidth(undefined)).toBe(false);
  });
});

describe("sellSplit", () => {
  it("splits automatic (10b5-1) from open-market sells", () => {
    const t = (transaction: string) => ({ insider: "x", relation: null, date: null, transaction, shares: 1, price: 1, value: 1, shares_held: 1 });
    expect(sellSplit([t("Automatic Sell"), t("Automatic Sell"), t("Sell"), t("Buy"), t("Option Execute")])).toEqual({ automatic: 2, openMarket: 1, total: 3 });
  });
});
