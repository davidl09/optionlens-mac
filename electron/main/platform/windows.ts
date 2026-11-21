// Windows: launch at login is the HKCU Run key that Electron writes (the user can disable it in Task
// Manager → Startup apps, which Electron reports as executableWillLaunchAtLogin = false); notifications
// need the AppUserModelID that the installer put on the Start-menu shortcut; .ico tray icons.
import { APP_ID } from "../app-id";
import type { LoginItemStatus } from "../../shared/api";
import { standardMenu } from "./menu";
import { windowsPython } from "./python-layout";
import { OPENED_AT_LOGIN_ARG, type AppLike, type DesktopPlatform, type PlatformDeps } from "./types";

export function windowsLoginStatus(app: AppLike, exe: string): LoginItemStatus {
  const s = app.getLoginItemSettings({ path: exe, args: [OPENED_AT_LOGIN_ARG] });
  return s.openAtLogin && s.executableWillLaunchAtLogin !== false ? "enabled" : "disabled";
}

export function windowsPlatform(d: PlatformDeps): DesktopPlatform {
  return {
    ...windowsPython,
    loginItem: {
      status: () => windowsLoginStatus(d.app, d.execPath),
      set: (on) => d.app.setLoginItemSettings({ openAtLogin: on, path: d.execPath, args: [OPENED_AT_LOGIN_ARG] }),
      openedAtLogin: () => d.argv.includes(OPENED_AT_LOGIN_ARG),
    },
    trayIcons: async () => ({ idle: "tray.ico", connected: "trayConnected.ico", template: false }),
    initNotifications: () => d.app.setAppUserModelId(APP_ID),
    appMenu: standardMenu,
  };
}
