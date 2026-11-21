import { describe, expect, it, vi } from "vitest";
import { APP_ID } from "../app-id";
import { desktopPlatform } from "./index";
import { autostartEnabled, autostartEntry, desktopExecArg, hasStatusNotifierWatcher, launcherPath } from "./linux";
import { macLoginStatus } from "./mac";
import { OPENED_AT_LOGIN_ARG, type AppLike, type PlatformDeps, type RunFn } from "./types";

function deps(over: Partial<PlatformDeps> = {}) {
  const files = new Map<string, string>();
  const app = {
    settings: { openAtLogin: false } as ReturnType<AppLike["getLoginItemSettings"]>,
    getLoginItemSettings: vi.fn((_o?: { path?: string; args?: string[] }) => app.settings),
    setLoginItemSettings: vi.fn(),
    setAppUserModelId: vi.fn(),
  };
  const d: PlatformDeps = {
    app, argv: ["/opt/OptionLens"], env: {}, execPath: "/opt/OptionLens/optionlens", packaged: true, home: "/home/me",
    files: { read: (p) => files.get(p) ?? null, write: (p, c) => { files.set(p, c); }, remove: (p) => { files.delete(p); } },
    run: async () => { throw new Error("no dbus"); },
    ...over,
  };
  return { d, app, files };
}

describe("macOS", () => {
  it("reports what macOS has, including approval pending and items disabled in System Settings", () => {
    expect(macLoginStatus({ openAtLogin: true, status: "enabled" })).toBe("enabled");
    expect(macLoginStatus({ openAtLogin: true, status: "requires-approval" })).toBe("requires-approval");
    expect(macLoginStatus({ openAtLogin: false, status: "not-registered" })).toBe("disabled");
    expect(macLoginStatus({ openAtLogin: false, status: "not-found" })).toBe("disabled");
    expect(macLoginStatus({ openAtLogin: true })).toBe("enabled");      // macOS 12
  });

  it("uses the login item, wasOpenedAtLogin and template tray icons", async () => {
    const { d, app } = deps();
    const p = desktopPlatform("darwin", d);
    p.loginItem.set(true);
    expect(app.setLoginItemSettings).toHaveBeenCalledWith({ openAtLogin: true });
    app.settings = { openAtLogin: true, wasOpenedAtLogin: true };
    expect(p.loginItem.openedAtLogin()).toBe(true);
    expect(await p.trayIcons()).toEqual({ idle: "trayTemplate.png", connected: "trayConnectedTemplate.png", template: true });
    expect(p.bundledPython("/A/Contents/Resources")).toBe("/A/Contents/Resources/python/bin/python3");
    expect(p.appMenu({ openSettings: () => undefined })[0].role).toBe("appMenu");
  });
});

describe("Windows", () => {
  const exe = "C:\\Users\\Jane Doe\\AppData\\Local\\Programs\\OptionLens\\OptionLens.exe";

  it("registers the Run key for this exe with the at-login argument and reads it back the same way", () => {
    const { d, app } = deps({ execPath: exe, argv: [exe] });
    const p = desktopPlatform("win32", d);
    p.loginItem.set(true);
    expect(app.setLoginItemSettings).toHaveBeenCalledWith({ openAtLogin: true, path: exe, args: [OPENED_AT_LOGIN_ARG] });
    app.settings = { openAtLogin: true, executableWillLaunchAtLogin: true };
    expect(p.loginItem.status()).toBe("enabled");
    expect(app.getLoginItemSettings).toHaveBeenLastCalledWith({ path: exe, args: [OPENED_AT_LOGIN_ARG] });
    app.settings = { openAtLogin: true, executableWillLaunchAtLogin: false };   // disabled in Task Manager → Startup apps
    expect(p.loginItem.status()).toBe("disabled");
    app.settings = { openAtLogin: false, executableWillLaunchAtLogin: false };
    expect(p.loginItem.status()).toBe("disabled");
    expect(p.loginItem.openedAtLogin()).toBe(false);
    expect(desktopPlatform("win32", { ...d, argv: [exe, OPENED_AT_LOGIN_ARG] }).loginItem.openedAtLogin()).toBe(true);
  });

  it("sets the AppUserModelID before notifications and uses .ico tray icons and a File menu", async () => {
    const { d, app } = deps({ execPath: exe });
    const p = desktopPlatform("win32", d);
    p.initNotifications();
    expect(app.setAppUserModelId).toHaveBeenCalledWith(APP_ID);
    expect(await p.trayIcons()).toEqual({ idle: "tray.ico", connected: "trayConnected.ico", template: false });
    expect(p.appMenu({ openSettings: () => undefined })[0].label).toBe("&File");
  });
});

describe("Linux", () => {
  it("quotes Exec arguments as the Desktop Entry Specification says", () => {
    expect(desktopExecArg("/home/me/.local/bin/OptionLens.AppImage")).toBe("/home/me/.local/bin/OptionLens.AppImage");
    expect(desktopExecArg("/home/me/My Apps/OptionLens.AppImage")).toBe(`"/home/me/My Apps/OptionLens.AppImage"`);
    // inside quotes " ` $ \ get a backslash; then every backslash is doubled (string escape); % → %%
    expect(desktopExecArg(`/a "b" $c \`d\` \\e 100%`)).toBe(`"/a \\\\"b\\\\" \\\\$c \\\\\`d\\\\\` \\\\\\\\e 100%%"`);
    expect(() => desktopExecArg("/a\nExec=evil")).toThrow(/control/);
  });

  it("writes and removes the XDG autostart entry for the AppImage and reads its state", () => {
    const { d, files } = deps({ env: { APPIMAGE: "/home/me/Apps dir/OptionLens.AppImage" } });
    const p = desktopPlatform("linux", d);
    expect(p.loginItem.status()).toBe("disabled");
    p.loginItem.set(true);
    const entry = files.get("/home/me/.config/autostart/optionlens.desktop")!;
    expect(entry).toBe(autostartEntry("/home/me/Apps dir/OptionLens.AppImage"));
    expect(entry).toContain(`Exec="/home/me/Apps dir/OptionLens.AppImage" ${OPENED_AT_LOGIN_ARG}\n`);
    expect(entry.startsWith("[Desktop Entry]\nType=Application\nName=OptionLens\n")).toBe(true);
    expect(p.loginItem.status()).toBe("enabled");
    files.set("/home/me/.config/autostart/optionlens.desktop", `${entry}Hidden=true\n`);   // turned off in the desktop's startup settings
    expect(p.loginItem.status()).toBe("disabled");
    p.loginItem.set(false);
    expect(files.size).toBe(0);
  });

  it("honours XDG_CONFIG_HOME; unsupported in development builds", () => {
    const { d, files } = deps({ env: { XDG_CONFIG_HOME: "/cfg" } });
    desktopPlatform("linux", d).loginItem.set(true);
    expect([...files.keys()]).toEqual(["/cfg/autostart/optionlens.desktop"]);
    expect(files.get("/cfg/autostart/optionlens.desktop")).toContain("Exec=/opt/OptionLens/optionlens --opened-at-login");
    expect(desktopPlatform("linux", deps({ packaged: false }).d).loginItem.status()).toBe("unsupported");
    expect(launcherPath({ packaged: true, execPath: "/x/optionlens", env: { APPIMAGE: "relative.AppImage" } })).toBe("/x/optionlens");
  });

  it("parses the autostart switches", () => {
    expect(autostartEnabled("[Desktop Entry]\nExec=x\n")).toBe(true);
    expect(autostartEnabled("[Desktop Entry]\nX-GNOME-Autostart-enabled=false\n")).toBe(false);
    expect(autostartEnabled("[Desktop Entry]\nHidden = true\n")).toBe(false);
    expect(autostartEnabled("[Desktop Action x]\nHidden=true\n[Desktop Entry]\nExec=x\n")).toBe(true);
  });

  it("has a tray only with a StatusNotifierWatcher on the session bus (gdbus, else dbus-send)", async () => {
    const answer = (out: Record<string, string>): RunFn => async (file) => { if (!(file in out)) throw new Error("ENOENT"); return out[file]; };
    expect(await hasStatusNotifierWatcher(answer({ gdbus: "(true,)\n" }))).toBe(true);
    expect(await hasStatusNotifierWatcher(answer({ gdbus: "(false,)\n", "dbus-send": "boolean true" }))).toBe(false);
    expect(await hasStatusNotifierWatcher(answer({ "dbus-send": "method return …\n   boolean true\n" }))).toBe(true);
    expect(await hasStatusNotifierWatcher(answer({}))).toBe(false);
    const withTray = desktopPlatform("linux", deps({ run: answer({ gdbus: "(true,)" }) }).d);
    expect(await withTray.trayIcons()).toEqual({ idle: "tray.png", connected: "trayConnected.png", template: false });
    expect(await desktopPlatform("linux", deps().d).trayIcons()).toBeNull();
  });
});

describe("platform choice", () => {
  it("fails fast on an OS OptionLens does not support", () => {
    expect(() => desktopPlatform("freebsd", deps().d)).toThrow(/does not run on freebsd/);
  });
});
