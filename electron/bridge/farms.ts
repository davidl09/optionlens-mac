// IBKR's server farms as IB Gateway / TWS reports them in connection-level messages (reqId -1): market data,
// historical data (HMDS) and security definitions (sec-def: contract details and option parameters). Each farm
// can have several servers ("usfarm", "usopt", ...); a farm is as bad as its worst server. Pure state.
import type { TwsSystemMessage } from "./tws";

export type Farm = "marketData" | "historical" | "secDef";
/** "inactive": idle but available on demand (2107 / 2108); "connecting": not usable yet (2119). */
export type FarmState = "ok" | "inactive" | "connecting" | "down" | "unknown";
export interface FarmStatus { state: FarmState; since: string | null; server: string | null }
export type FarmsStatus = Record<Farm, FarmStatus>;

export const FARMS: readonly Farm[] = ["marketData", "historical", "secDef"];

/** IBKR message codes of farm connection state (TWS API "message codes"). */
const BY_CODE: Record<number, { farm: Farm; state: FarmState }> = {
  2103: { farm: "marketData", state: "down" },        // Market data farm connection is broken:<server>
  2104: { farm: "marketData", state: "ok" },          // Market data farm connection is OK:<server>
  2105: { farm: "historical", state: "down" },        // HMDS data farm connection is broken:<server>
  2106: { farm: "historical", state: "ok" },          // HMDS data farm connection is OK:<server>
  2107: { farm: "historical", state: "inactive" },    // HMDS data farm connection is inactive but should be available upon demand
  2108: { farm: "marketData", state: "inactive" },    // Market data farm connection is inactive but should be available upon demand
  2119: { farm: "marketData", state: "connecting" },  // Market data farm is connecting:<server>
  2157: { farm: "secDef", state: "down" },            // Sec-def data farm connection is broken:<server>
  2158: { farm: "secDef", state: "ok" },              // Sec-def data farm connection is OK:<server>
};

/**
 * The farm, state and server a message reports, or null for any other message. The state comes from the code;
 * the farm from the text where it names one (the wording of a code differs between TWS versions), else the code.
 */
export function parseFarmMessage(m: TwsSystemMessage): { farm: Farm; state: FarmState; server: string | null } | null {
  const known = BY_CODE[m.code];
  if (!known) return null;
  const text = m.message.toLowerCase();
  const farm: Farm = /sec-?def/.test(text) ? "secDef" : /hmds|historical/.test(text) ? "historical" : /market ?data/.test(text) ? "marketData" : known.farm;
  const server = /:\s*([\w.-]+)\s*$/.exec(m.message)?.[1] ?? null;
  return { farm, state: known.state, server };
}

const SEVERITY: Record<FarmState, number> = { down: 4, connecting: 3, ok: 2, inactive: 1, unknown: 0 };

export class FarmMonitor {
  private readonly servers = new Map<Farm, Map<string, { state: FarmState; since: number }>>();

  constructor(private readonly now: () => number = Date.now) {}

  /** Applies a connection-level message; returns the farm whose state changed, else null. */
  apply(m: TwsSystemMessage): Farm | null {
    const p = parseFarmMessage(m);
    if (!p) return null;
    const before = this.status(p.farm).state;
    const servers = this.servers.get(p.farm) ?? new Map<string, { state: FarmState; since: number }>();
    this.servers.set(p.farm, servers);
    const key = p.server ?? "";
    if (servers.get(key)?.state !== p.state) servers.set(key, { state: p.state, since: this.now() });
    return this.status(p.farm).state === before ? null : p.farm;
  }

  /** The farm's worst server state, since when that server has been in it, and its name. */
  status(farm: Farm): { state: FarmState; since: number | null; server: string | null } {
    let worst: { state: FarmState; since: number | null; server: string | null } = { state: "unknown", since: null, server: null };
    for (const [server, s] of this.servers.get(farm) ?? []) {
      if (SEVERITY[s.state] > SEVERITY[worst.state] || (s.state === worst.state && worst.since !== null && s.since < worst.since)) {
        worst = { state: s.state, since: s.since, server: server || null };
      }
    }
    return worst;
  }

  snapshot(): FarmsStatus {
    const one = (f: Farm): FarmStatus => {
      const s = this.status(f);
      return { state: s.state, since: s.since === null ? null : new Date(s.since).toISOString(), server: s.server };
    };
    return { marketData: one("marketData"), historical: one("historical"), secDef: one("secDef") };
  }

  /** A new session: nothing is known until the Gateway reports again. */
  reset(): void { this.servers.clear(); }
}

/** After an unanswered contract-definition request, how long to wait before asking (probing) once more. */
export const SECDEF_PROBE_MS = 15 * 60_000;

/**
 * Whether contract definitions (sec-def) can be asked for now: not while the Gateway reports the sec-def farm
 * broken or connecting (until it reports it OK), nor for SECDEF_PROBE_MS after a request went unanswered (the
 * Gateway does not always report the farm; the next request after that is the probe). A scheduler gate for the
 * chain tasks parked on it.
 */
export class ContractServer {
  private unansweredAt: number | null = null;

  constructor(private readonly farm: () => FarmState, private readonly now: () => number = Date.now) {}

  /** A contract-definition request went unanswered (timeout). */
  unanswered(): void { this.unansweredAt = this.now(); }
  /** A contract-definition request was answered, or the Gateway reported the farm OK. */
  answered(): void { this.unansweredAt = null; }

  readyAt(): number {
    const s = this.farm();
    if (s === "down" || s === "connecting") return Infinity;
    return this.unansweredAt === null ? 0 : this.unansweredAt + SECDEF_PROBE_MS;
  }

  available(): boolean { return this.readyAt() <= this.now(); }

  /** Since when requests went unanswered (null: none outstanding, or the probe time has come). */
  unansweredSince(): number | null { return this.available() ? null : this.unansweredAt; }
}
