// macOS: the login item is an SMAppService (macOS 13+) that the user can disable in System Settings →
// General → Login Items, or that waits for their approval; menu-bar template icons; the app menu.
import type { LoginItemStatus } from "../../shared/api";
import { macMenu } from "./menu";
import { posixPython } from "./python-layout";
import type { DesktopPlatform, PlatformDeps } from "./types";

/** What app.getLoginItemSettings() reports on macOS (status: macOS 13+ only). */
export function macLoginStatus(s: { openAtLogin: boolean; status?: string }): LoginItemStatus {
  switch (s.status) {
    case "enabled": return "enabled";
    case "requires-approval": return "requires-approval";
    case "not-registered":
    case "not-found": return "disabled";
    default: return s.openAtLogin ? "enabled" : "disabled";   // macOS 12: no status
  }
}

export function macPlatform(d: PlatformDeps): DesktopPlatform {
  return {
    ...posixPython,
    loginItem: {
      status: () => macLoginStatus(d.app.getLoginItemSettings()),
      set: (on) => d.app.setLoginItemSettings({ openAtLogin: on }),
      openedAtLogin: () => d.app.getLoginItemSettings().wasOpenedAtLogin === true,
    },
    trayIcons: async () => ({ idle: "trayTemplate.png", connected: "trayConnectedTemplate.png", template: true }),
    initNotifications: () => undefined,
    appMenu: macMenu,
  };
}
