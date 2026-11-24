import { describe, expect, it } from "vitest";
import { mixedCurrencies, rowMoney } from "../components/Money";
import { fmt, USD_STYLE } from "./format";
import { bridgeServes, currencyOf, exchangeOf, marketOf, moneyStyleOf } from "./markets";

describe("markets", () => {
  it("keys a US listing and its Toronto listing apart", () => {
    expect([marketOf("RY"), currencyOf("RY"), exchangeOf("RY")]).toEqual(["US", "USD", null]);
    expect([marketOf("RY.TO"), currencyOf("RY.TO"), exchangeOf("RY.TO")]).toEqual(["CA", "CAD", "TSX"]);
    expect([marketOf("RCI.B.TO"), exchangeOf("ABC.VN"), marketOf("BRK.B"), marketOf("TO")]).toEqual(["CA", "TSXV", "US", "US"]);
    expect([bridgeServes("AAPL"), bridgeServes("TD.TO")]).toEqual([true, false]);
  });
});

describe("money labels", () => {
  it("shows CAD as C$, USD as $ unless the context is mixed or Canadian", () => {
    expect(fmt(12.5, "money")).toBe("$12.50");
    expect(fmt(-12.5, "money", moneyStyleOf("RY.TO"))).toBe("-C$12.50");
    expect(fmt(12.5, "money", { currency: "USD", explicit: true })).toBe("US$12.50");
    expect(fmt(2.9e11, "bigmoney")).toBe("290.00B");
    expect(fmt(2.9e11, "bigmoney", moneyStyleOf("RY.TO"))).toBe("C$290.00B");
    expect(fmt(-1.5e9, "bigmoney", { currency: "USD", explicit: true })).toBe("-US$1.50B");
    expect(fmt(0.25, "pct", moneyStyleOf("RY.TO"))).toBe("25.0%");      // only money is labelled
  });
  it("labels each report row in its own currency, US$ when a table mixes them", () => {
    const rows = [{ symbol: "AAPL", currency: "USD" }, { symbol: "RY.TO", currency: "CAD" }];
    expect(mixedCurrencies(rows)).toBe(true);
    expect(mixedCurrencies([rows[0]])).toBe(false);
    const base = { ...USD_STYLE, explicit: true };
    expect(fmt(1, "money", rowMoney(base, rows[0]))).toBe("US$1.00");
    expect(fmt(1, "money", rowMoney(base, rows[1]))).toBe("C$1.00");
    expect(fmt(1, "money", rowMoney(USD_STYLE, { symbol: "SECTOR" }))).toBe("$1.00");
  });
});
