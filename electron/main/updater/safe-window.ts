// When an automatic update may restart the app. Never while the engine collects the closing chains
// (16:15–16:50 New York time on weekdays) or while a data import runs. Otherwise either the user asked
// for it (Restart now) or the Mac has been idle for 5 minutes with no OptionLens window focused.

export const CLOSE_WINDOW = { from: 16 * 60 + 15, to: 16 * 60 + 50 } as const;
export const IDLE_SECONDS = 5 * 60;

const NY_CLOCK = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** Weekday (0 = Sunday) and minutes since midnight in New York (DST handled by Intl). */
export function nyClock(d: Date): { weekday: number; minutes: number } {
  const p = Object.fromEntries(NY_CLOCK.formatToParts(d).map((x) => [x.type, x.value]));
  return { weekday: WEEKDAYS.indexOf(p.weekday), minutes: Number(p.hour) * 60 + Number(p.minute) };
}

export function inCloseCollectionWindow(now: Date): boolean {
  const { weekday, minutes } = nyClock(now);
  return weekday >= 1 && weekday <= 5 && minutes >= CLOSE_WINDOW.from && minutes < CLOSE_WINDOW.to;
}

export interface InstallContext {
  now: Date;
  importing: boolean;
  idleSeconds: number;
  windowFocused: boolean;
}

/** null when the app may restart into the update now, else why not (shown to the user). */
export function installBlocker(c: InstallContext, userInitiated: boolean): string | null {
  if (c.importing) return "A data import is running; the update installs after it finishes.";
  if (inCloseCollectionWindow(c.now)) return "The engine is collecting closing data (16:15–16:50 ET); the update installs after that.";
  if (userInitiated) return null;
  if (c.windowFocused || c.idleSeconds < IDLE_SECONDS) return "Waiting until the Mac is idle to restart into the update.";
  return null;
}
