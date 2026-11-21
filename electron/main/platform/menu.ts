// Application menus: macOS has the app menu in the menu bar; Windows and Linux show a File menu in the window.
import type { MenuItemConstructorOptions } from "electron";
import type { MenuActions } from "./types";

const settingsItem = (a: MenuActions): MenuItemConstructorOptions => ({ label: "Settings…", accelerator: "CommandOrControl+,", click: () => a.openSettings() });
const common: MenuItemConstructorOptions[] = [{ role: "editMenu" }, { role: "viewMenu" }, { role: "windowMenu" }];

export function macMenu(a: MenuActions): MenuItemConstructorOptions[] {
  return [
    { role: "appMenu", submenu: [
      { role: "about" }, { type: "separator" }, settingsItem(a),
      { type: "separator" }, { role: "hide" }, { role: "hideOthers" }, { role: "unhide" }, { type: "separator" }, { role: "quit" },
    ] },
    ...common,
  ];
}

export function standardMenu(a: MenuActions): MenuItemConstructorOptions[] {
  return [
    { label: "&File", submenu: [settingsItem(a), { type: "separator" }, { role: "quit", label: "Quit OptionLens", accelerator: "CommandOrControl+Q" }] },
    ...common,
  ];
}
