// The main window: locked-down renderer (context isolation, no Node, sandbox), app:// only navigation,
// external links in the default browser, and hide-on-close (while `shouldReallyClose` is false) so the app
// keeps running in the tray. Hidden or minimized, the renderer stops its live quotes (background throttling).
import { BrowserWindow, shell } from "electron";
import { APP_ORIGIN } from "./protocol";

export function createMainWindow(preload: string, shouldReallyClose: () => boolean): BrowserWindow {
  const win = new BrowserWindow({
    width: 1440, height: 920, minWidth: 900, minHeight: 600, show: false, title: "OptionLens",
    backgroundColor: "#0f1115",
    webPreferences: {
      preload, contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true, spellcheck: false,
      // A hidden (tray) or minimized window must report document.visibilityState "hidden": the renderer then
      // unsubscribes its live quotes, so the engine stops fetching them (src/live/connection.ts). Keep this on.
      backgroundThrottling: true,
    },
  });
  win.webContents.on("will-navigate", (e, url) => { if (!url.startsWith(`${APP_ORIGIN}/`)) e.preventDefault(); });
  win.webContents.on("will-attach-webview", (e) => e.preventDefault());
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  win.on("close", (e) => {
    if (shouldReallyClose()) return;
    e.preventDefault();
    win.hide();
  });
  win.once("ready-to-show", () => win.show());
  void win.loadURL(`${APP_ORIGIN}/`);
  return win;
}
