// Where the running app lives, and whether an update can replace it there (one Location per OS; the
// Windows and Linux checks live in installers/). On macOS an app opened from a quarantined download runs
// from a random read-only copy (App Translocation); a dmg or another read-only volume cannot be written
// either. In those cases the app must be moved to /Applications first.
import { dirname, sep } from "node:path";

/** appPath: the .app bundle (macOS), the AppImage file (Linux) or OptionLens.exe (Windows). */
export type Location = { ok: true; appPath: string } | { ok: false; reason: string };

export const DEVELOPMENT_BUILD = "Updates are installed only by the packaged app (this is a development build).";
export const MOVE_TO_APPLICATIONS = "Move OptionLens to the Applications folder first (then open it from there), so it can update itself.";

/** …/OptionLens.app/Contents/MacOS/OptionLens → …/OptionLens.app, else null (not a macOS app bundle). */
export function bundlePathFromExecutable(execPath: string): string | null {
  const parts = execPath.split(sep);
  const i = parts.length - 4;
  if (i < 1 || !parts[i].endsWith(".app") || parts[i + 1] !== "Contents" || parts[i + 2] !== "MacOS") return null;
  return parts.slice(0, i + 1).join(sep);
}

export function isTranslocated(bundlePath: string): boolean {
  return bundlePath.includes("/AppTranslocation/");
}

export interface LocationProbe {
  packaged: boolean;
  execPath: string;
  /** True when this process may create and remove entries in `dir` (fs.accessSync W_OK). */
  writable: (dir: string) => boolean;
}

export function macLocation(p: LocationProbe): Location {
  if (!p.packaged) return { ok: false, reason: DEVELOPMENT_BUILD };
  const bundlePath = bundlePathFromExecutable(p.execPath);
  if (!bundlePath) return { ok: false, reason: "OptionLens is not running from an app bundle." };
  if (isTranslocated(bundlePath) || bundlePath.startsWith("/Volumes/") || !p.writable(dirname(bundlePath)) || !p.writable(bundlePath)) {
    return { ok: false, reason: MOVE_TO_APPLICATIONS };
  }
  return { ok: true, appPath: bundlePath };
}
