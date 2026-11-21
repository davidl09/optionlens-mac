// What differs between macOS, Windows and Linux for the desktop app, behind one small contract. Each OS has
// one module (mac.ts, windows.ts, linux.ts); index.ts picks it. Nothing else in the main process asks which
// OS it runs on. The modules take Electron's `app` and the file system as dependencies, so they are unit-tested.
import type { MenuItemConstructorOptions } from "electron";
import type { LoginItemStatus } from "../../shared/api";

export interface LoginItem {
  /** What the OS has (the user may have changed it outside OptionLens). */
  status(): LoginItemStatus;
  set(on: boolean): void;
  /** This launch was started by the OS at login (the window then stays hidden in the tray). */
  openedAtLogin(): boolean;
}

/** Tray icon files in the app's resources folder; template: macOS menu-bar template images. */
export interface TrayIcons { idle: string; connected: string; template: boolean }

export interface MenuActions { openSettings(): void }

export interface DesktopPlatform {
  /** The bundled engine's Python inside the app's resources folder. */
  bundledPython(resourcesPath: string): string;
  /** The Python of a development virtualenv (`engine/.venv`). */
  venvPython(venvDir: string): string;
  loginItem: LoginItem;
  /** The tray icons, or null when this desktop shows no tray: closing the window then quits the app. */
  trayIcons(): Promise<TrayIcons | null>;
  /** Once, before the first notification. */
  initNotifications(): void;
  appMenu(a: MenuActions): MenuItemConstructorOptions[];
}

/** The parts of Electron's `app` the platform modules use. */
export interface AppLike {
  getLoginItemSettings(o?: { path?: string; args?: string[] }): { openAtLogin: boolean; status?: string; wasOpenedAtLogin?: boolean; executableWillLaunchAtLogin?: boolean };
  setLoginItemSettings(s: { openAtLogin: boolean; path?: string; args?: string[] }): void;
  setAppUserModelId(id: string): void;
}

export interface TextFiles {
  read(path: string): string | null;
  write(path: string, content: string): void;
  remove(path: string): void;
}

/** Runs a program (no shell); resolves with stdout, rejects on failure, a non-zero exit or the timeout. */
export type RunFn = (file: string, args: string[], timeoutMs: number) => Promise<string>;

export interface PlatformDeps {
  app: AppLike;
  argv: string[];
  env: Record<string, string | undefined>;
  execPath: string;
  packaged: boolean;
  home: string;
  files: TextFiles;
  run: RunFn;
}

/** Added to the login item's command line, so a launch at login is recognised (Windows, Linux). */
export const OPENED_AT_LOGIN_ARG = "--opened-at-login";
