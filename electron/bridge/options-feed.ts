// What the account's US options market data allows (persisted). An account without an OPRA subscription gets delayed
// option quotes during the regular session, but TWS refuses delayed-frozen (type 4) option data outside it (error 354
// on a weekend). After such a refusal the account is "delayed_only": option quotes are asked for only during regular
// hours, as delayed data (type 3); outside them option market data is not requested at all. Once per trading day,
// the first option request of the session goes out as before (type 4): real-time option quotes end the mode.
import { nyDate, nyRegularHours } from "./ny";

export type OptionsSubscription = "unknown" | "realtime" | "delayed_only";

export interface OptionsFeedStore { read(): unknown; write(v: unknown): void }

interface State {
  mode: OptionsSubscription;
  /** When the mode last changed (ISO). */
  since: string | null;
  /** NY date of the day's probe (type 4 request) while delayed_only. */
  probedDay: string | null;
  /** NY date on which even delayed option data was refused during regular hours (e.g. a market holiday). */
  refusedDay: string | null;
}

/** Market data type for an option request: 3 = delayed; undefined = the session's default (4, delayed-frozen). */
export type OptionDataType = 3 | undefined;

const MODES: readonly OptionsSubscription[] = ["unknown", "realtime", "delayed_only"];

export class OptionsFeed {
  private s: State = { mode: "unknown", since: null, probedDay: null, refusedDay: null };

  constructor(private readonly store: OptionsFeedStore | null) {
    const raw = store?.read() as Partial<State> | null | undefined;
    if (raw && typeof raw === "object" && MODES.includes(raw.mode as OptionsSubscription)) {
      const str = (v: unknown) => (typeof v === "string" ? v : null);
      this.s = { mode: raw.mode as OptionsSubscription, since: str(raw.since), probedDay: str(raw.probedDay), refusedDay: str(raw.refusedDay) };
    }
  }

  get mode(): OptionsSubscription { return this.s.mode; }
  get since(): string | null { return this.s.since; }

  /** Whether option market data may be requested now (false: skip option quotes, they would be refused). */
  canQuote(now: Date): boolean {
    if (this.s.mode !== "delayed_only") return true;
    return nyRegularHours(now) && this.s.refusedDay !== nyDate(now);
  }

  /** The market data type for option requests now: the day's first one is the probe (session default). */
  dataType(now: Date): OptionDataType {
    return this.s.mode === "delayed_only" && this.s.probedDay === nyDate(now) ? 3 : undefined;
  }

  /** TWS refused option market data for lack of a subscription (354 / 10089 / 10090 / 10168). */
  refused(now: Date, dataType: OptionDataType): void {
    const day = nyDate(now);
    if (this.s.mode !== "delayed_only") this.set({ mode: "delayed_only", since: now.toISOString(), probedDay: nyRegularHours(now) ? day : null, refusedDay: null });
    else if (dataType === undefined && nyRegularHours(now)) this.set({ probedDay: day });   // the probe: delayed (type 3) next
    else if (nyRegularHours(now)) this.set({ refusedDay: day });                            // even delayed refused: no options today
  }

  /** Option quotes arrived (`delayed`: from delayed tick types) with market data type `dataType`. */
  quoted(now: Date, delayed: boolean, dataType: OptionDataType): void {
    if (!delayed && this.s.mode !== "realtime") { this.set({ mode: "realtime", since: now.toISOString(), probedDay: null, refusedDay: null }); return; }
    if (this.s.mode === "delayed_only" && dataType === undefined && nyRegularHours(now)) this.set({ probedDay: nyDate(now) });
  }

  private set(p: Partial<State>): void {
    this.s = { ...this.s, ...p };
    try { this.store?.write(this.s); } catch { /* not persisted: re-learned after a restart */ }
  }
}
