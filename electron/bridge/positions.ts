// The IBKR positions feed (INTEGRATION.md §6 POST /api/ingest/positions, §11): while the engine wants it (live-wants
// `positions`, the user's "Show my positions" for IBKR) and TWS is connected, the bridge keeps two read-only account
// subscriptions open — reqPositions (every account's positions, then each change) and reqAccountSummary (net
// liquidation, cash, buying power and, via $LEDGER:ALL, the exchange rate of each currency to the base currency) — and
// pushes the whole picture to the engine once the first position list is complete, DEBOUNCE_MS after a change, and
// every PUSH_MS. Nothing here places, changes or reads orders. Account numbers are never logged (maskAccount).
import type { Logger } from "../main/log";
import { maskAccount } from "./mask";
import type { IngestApi, IngestPosition, IngestPositions } from "./ingest";
import type { AccountValue, TwsPosition, TwsSession } from "./tws";

export const PUSH_MS = 60_000;
export const DEBOUNCE_MS = 1_000;
export const SUMMARY_TAGS = "NetLiquidation,TotalCashValue,BuyingPower,$LEDGER:ALL";

const SUMMARY_FIELDS: Record<string, "net_liquidation" | "cash" | "buying_power"> = {
  NetLiquidation: "net_liquidation", TotalCashValue: "cash", BuyingPower: "buying_power",
};

export interface PositionsDeps {
  ingest: Pick<IngestApi, "positions">;
  log: Logger;
  now?: () => number;
  pushMs?: number;
  debounceMs?: number;
}

const iso = (yyyymmdd: string | null): string | null =>
  yyyymmdd && /^\d{8}$/.test(yyyymmdd) ? `${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6, 8)}` : null;

const num = (v: string): number | null => (v.trim() !== "" && Number.isFinite(Number(v)) ? Number(v) : null);

/** A TWS position as the ingest body's row. */
export function positionRow(p: TwsPosition): IngestPosition {
  const c = p.contract;
  return {
    account: p.account, sec_type: c.secType, symbol: c.symbol, currency: (c.currency ?? "USD").toUpperCase(), qty: p.pos, avg_cost: p.avgCost,
    multiplier: c.multiplier, expiry: iso(c.expiry), strike: c.strike, right: c.right, trading_class: c.tradingClass, con_id: c.conId,
    ...(c.exchange ? { exchange: c.exchange } : {}),
  };
}

export class PositionsFeed {
  private session: TwsSession | null = null;
  private cancels: (() => void)[] = [];
  private readonly positions = new Map<string, TwsPosition>();
  private readonly summary = new Map<string, { currency: string; net_liquidation: number | null; cash: number | null; buying_power: number | null }>();
  /** account -> currency -> base-currency value of 1 unit. */
  private readonly rates = new Map<string, Map<string, number>>();
  private ready = false;
  private debounce: ReturnType<typeof setTimeout> | null = null;
  private interval: ReturnType<typeof setInterval> | null = null;
  private readonly now: () => number;
  lastPushAt: number | null = null;

  constructor(private readonly d: PositionsDeps) {
    this.now = d.now ?? Date.now;
  }

  get active(): boolean { return this.session !== null; }

  /** Subscribed while `wanted` and connected (a new session subscribes anew); else everything is cancelled and forgotten. */
  update(session: TwsSession | null, wanted: boolean): void {
    if (!wanted || !session) { this.stop(); return; }
    if (this.session === session) return;
    this.stop();
    this.start(session);
  }

  stop(): void {
    for (const c of this.cancels) { try { c(); } catch { /* ignore */ } }
    this.cancels = [];
    if (this.debounce) clearTimeout(this.debounce);
    if (this.interval) clearInterval(this.interval);
    this.debounce = this.interval = null;
    if (this.session) this.d.log.info("bridge: positions feed stopped");
    this.session = null;
    this.positions.clear();
    this.summary.clear();
    this.rates.clear();
    this.ready = false;
  }

  private start(s: TwsSession): void {
    this.session = s;
    this.d.log.info("bridge: positions feed started (read-only reqPositions / reqAccountSummary)");
    this.cancels.push(s.positions({
      onData: (p) => this.onPosition(p),
      onEnd: () => { this.ready = true; this.schedule(0); },
      onError: (e) => this.d.log.warn(`bridge: positions: ${e.message}`),
    }));
    this.cancels.push(s.accountSummary(SUMMARY_TAGS, {
      onData: (v) => this.onSummary(v),
      onEnd: () => this.schedule(this.d.debounceMs ?? DEBOUNCE_MS),
      onError: (e) => this.d.log.warn(`bridge: account summary: ${e.message}`),
    }));
    this.interval = setInterval(() => void this.push(), this.d.pushMs ?? PUSH_MS);
  }

  private onPosition(p: TwsPosition): void {
    const key = `${p.account}|${p.contract.conId ?? `${p.contract.symbol}:${p.contract.secType}:${p.contract.expiry}:${p.contract.strike}:${p.contract.right}`}`;
    if (p.pos === 0) this.positions.delete(key);
    else this.positions.set(key, p);
    if (this.ready) this.schedule(this.d.debounceMs ?? DEBOUNCE_MS);
  }

  private onSummary(v: AccountValue): void {
    const field = SUMMARY_FIELDS[v.tag];
    if (field && v.currency && v.currency !== "BASE") {
      const cur = this.summary.get(v.account) ?? { currency: v.currency.toUpperCase(), net_liquidation: null, cash: null, buying_power: null };
      cur[field] = num(v.value);
      cur.currency = v.currency.toUpperCase();
      this.summary.set(v.account, cur);
    } else if (v.tag === "ExchangeRate" && v.currency && v.currency !== "BASE") {
      const rate = num(v.value);
      if (rate !== null && rate > 0) {
        const m = this.rates.get(v.account) ?? new Map<string, number>();
        m.set(v.currency.toUpperCase(), rate);
        this.rates.set(v.account, m);
      }
    }
    if (this.ready) this.schedule(this.d.debounceMs ?? DEBOUNCE_MS);
  }

  private schedule(ms: number): void {
    if (this.debounce) clearTimeout(this.debounce);
    this.debounce = setTimeout(() => { this.debounce = null; void this.push(); }, ms);
  }

  /** The push body: every position, each account's summary and the exchange rates to its base currency. */
  body(): IngestPositions {
    const fx: IngestPositions["fx"] = [];
    const seen = new Set<string>();
    for (const [account, m] of this.rates) {
      const base = this.summary.get(account)?.currency;
      if (!base) continue;
      for (const [currency, rate] of m) {
        const k = `${currency}/${base}`;
        if (currency === base || seen.has(k)) continue;
        seen.add(k);
        fx.push({ currency, base, rate });
      }
    }
    return {
      source: "ibkr", fetched_at: new Date(this.now()).toISOString(),
      positions: [...this.positions.values()].map(positionRow),
      accounts: [...this.summary.entries()].map(([account, v]) => ({ account, ...v })),
      fx,
    };
  }

  async push(): Promise<void> {
    if (!this.session || !this.ready) return;
    const b = this.body();
    try {
      await this.d.ingest.positions(b);
      this.lastPushAt = this.now();
    } catch (e) {
      this.d.log.warn(`bridge: positions push failed (${b.positions.length} positions, accounts ${b.accounts.map((a) => maskAccount(a.account)).join(",") || "?"}): ${(e as Error).message}`);
    }
  }
}
