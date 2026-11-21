// Linux: launch at login is an XDG autostart entry ($XDG_CONFIG_HOME/autostart/optionlens.desktop) that runs
// the AppImage. The tray is a StatusNotifierItem (AppIndicator), shown only when the desktop runs a
// StatusNotifierWatcher (KDE, Cinnamon, XFCE, MATE, Ubuntu's GNOME; not plain GNOME): without one there
// is no tray, and closing the window quits the app. Notifications go through libnotify (Electron).
import { isAbsolute, join } from "node:path";
import type { LoginItemStatus } from "../../shared/api";
import { desktopEntryKeys } from "../desktop-entry";
import { standardMenu } from "./menu";
import { posixPython } from "./python-layout";
import { OPENED_AT_LOGIN_ARG, type DesktopPlatform, type PlatformDeps, type RunFn } from "./types";

export const AUTOSTART_FILE = "optionlens.desktop";

/** One argument of a desktop entry's Exec key (Desktop Entry Specification: quoting, then string escapes). */
export function desktopExecArg(arg: string): string {
  if (/[\0\n\r]/.test(arg)) throw new Error("a desktop entry argument cannot contain control characters");
  const quoted = /[\s"'\\><~|&;$*?#()`]/.test(arg) ? `"${arg.replace(/["`$\\]/g, (c) => `\\${c}`)}"` : arg;
  return quoted.replace(/\\/g, "\\\\").replace(/%/g, "%%");
}

export function autostartEntry(exec: string): string {
  return [
    "[Desktop Entry]",
    "Type=Application",
    "Name=OptionLens",
    "Comment=Options and stock analytics; keeps collecting in the background",
    `Exec=${desktopExecArg(exec)} ${OPENED_AT_LOGIN_ARG}`,
    "Icon=optionlens",
    "Terminal=false",
    "X-GNOME-Autostart-enabled=true",
    "",
  ].join("\n");
}

/** Whether an autostart entry runs at login (Hidden=true or X-GNOME-Autostart-enabled=false turn it off). */
export function autostartEnabled(content: string): boolean {
  const keys = desktopEntryKeys(content);
  return keys.get("Hidden") !== "true" && keys.get("X-GNOME-Autostart-enabled") !== "false";
}

/** What starts OptionLens: the AppImage ($APPIMAGE) or the packaged executable; null in development. */
export function launcherPath(d: Pick<PlatformDeps, "env" | "execPath" | "packaged">): string | null {
  if (!d.packaged) return null;
  const appImage = d.env.APPIMAGE;
  return appImage && isAbsolute(appImage) ? appImage : d.execPath;
}

export function autostartDir(d: Pick<PlatformDeps, "env" | "home">): string {
  const xdg = d.env.XDG_CONFIG_HOME;
  return join(xdg && isAbsolute(xdg) ? xdg : join(d.home, ".config"), "autostart");
}

const WATCHER = "org.kde.StatusNotifierWatcher";
const PROBES: [string, string[], RegExp][] = [
  ["gdbus", ["call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus",
    "--method", "org.freedesktop.DBus.NameHasOwner", WATCHER], /\(true,?\)/],
  ["dbus-send", ["--session", "--print-reply", "--dest=org.freedesktop.DBus", "/org/freedesktop/DBus",
    "org.freedesktop.DBus.NameHasOwner", `string:${WATCHER}`], /boolean true/],
];

/** Whether the session bus has a StatusNotifierWatcher (gdbus, else dbus-send; neither: no tray). */
export async function hasStatusNotifierWatcher(run: RunFn): Promise<boolean> {
  for (const [file, args, yes] of PROBES) {
    try { return yes.test(await run(file, args, 3000)); } catch { /* tool missing or no session bus: next */ }
  }
  return false;
}

export function linuxPlatform(d: PlatformDeps): DesktopPlatform {
  const file = join(autostartDir(d), AUTOSTART_FILE);
  const launcher = launcherPath(d);
  const status = (): LoginItemStatus => {
    if (!launcher) return "unsupported";
    const content = d.files.read(file);
    return content !== null && autostartEnabled(content) ? "enabled" : "disabled";
  };
  return {
    ...posixPython,
    loginItem: {
      status,
      set: (on) => {
        if (!launcher) return;
        if (on) d.files.write(file, autostartEntry(launcher)); else d.files.remove(file);
      },
      openedAtLogin: () => d.argv.includes(OPENED_AT_LOGIN_ARG),
    },
    trayIcons: async () => ((await hasStatusNotifierWatcher(d.run)) ? { idle: "tray.png", connected: "trayConnected.png", template: false } : null),
    initNotifications: () => undefined,
    appMenu: standardMenu,
  };
}
