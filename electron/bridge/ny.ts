// The one "today" of the bridge: the America/New_York calendar date (the engine's session-date
// convention). Never derive a trading date from UTC: after 19:00/20:00 ET the UTC date is tomorrow.
const NY_DATE = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });

/** YYYY-MM-DD in New York. */
export function nyDate(d: Date): string {
  return NY_DATE.format(d);
}

/** YYYYMMDD in New York (TWS expiry format). */
export function nyYmd(d: Date): string {
  return nyDate(d).replace(/-/g, "");
}

const NY_CLOCK = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

/** Inside a regular US session: Monday–Friday 09:30–16:00 New York time (exchange holidays are not known here). */
export function nyRegularHours(d: Date): boolean {
  const p = Object.fromEntries(NY_CLOCK.formatToParts(d).map((x) => [x.type, x.value]));
  if (p.weekday === "Sat" || p.weekday === "Sun") return false;
  const minutes = Number(p.hour) * 60 + Number(p.minute);
  return minutes >= 9 * 60 + 30 && minutes < 16 * 60;
}
