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
