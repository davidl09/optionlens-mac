// reqSecDefOptParams answers per underlying, cached for the New York day: the listed expiries and strikes change
// at most once a day (new weeklies are listed overnight), and on a busy TWS the request can take a minute.
import { nyDate } from "./ny";
import type { OptionParams } from "./tws";

export class OptionParamsCache {
  private readonly byConId = new Map<number, { day: string; params: OptionParams[] }>();

  get(conId: number, now: Date): OptionParams[] | null {
    const hit = this.byConId.get(conId);
    return hit && hit.day === nyDate(now) ? hit.params : null;
  }

  /** Only a non-empty answer is kept: "no options" is re-asked next time rather than trusted for a day. */
  set(conId: number, now: Date, params: OptionParams[]): void {
    if (params.length) this.byConId.set(conId, { day: nyDate(now), params });
    else this.byConId.delete(conId);
  }
}
