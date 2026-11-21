// The one place that picks the OS-specific module (see types.ts). Other OSes fail fast.
import { linuxPlatform } from "./linux";
import { macPlatform } from "./mac";
import type { DesktopPlatform, PlatformDeps } from "./types";
import { windowsPlatform } from "./windows";

export function desktopPlatform(os: NodeJS.Platform, d: PlatformDeps): DesktopPlatform {
  switch (os) {
    case "darwin": return macPlatform(d);
    case "win32": return windowsPlatform(d);
    case "linux": return linuxPlatform(d);
    default: throw new Error(`OptionLens does not run on ${os}.`);
  }
}

export type { DesktopPlatform, PlatformDeps, TrayIcons } from "./types";
