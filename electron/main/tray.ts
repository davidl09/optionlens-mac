// Tray icon (macOS menu bar, Windows notification area, Linux AppIndicator): engine + IBKR status and the
// app's background controls. Which icon files, and whether there is a tray at all, is per OS (platform/).
import { Menu, nativeImage, Tray, type MenuItemConstructorOptions } from "electron";
import { join } from "node:path";
import type { BridgeStatus, EngineStatus, LoginItemStatus, UpdateStatus } from "../shared/api";
import type { TrayIcons } from "./platform";
import { bridgeLines, engineLine, trayTooltip, updateMenuItem } from "./status-text";

export interface TrayActions {
  open(): void;
  openSettings(): void;
  setBridgeEnabled(on: boolean): void;
  cancelBridgeWork(): void;
  setLaunchAtLogin(on: boolean): void;
  installUpdate(): void;
  quit(): void;
}

export interface TrayState { engine: EngineStatus; bridge: BridgeStatus; loginItem: LoginItemStatus; update: UpdateStatus }

export class AppTray {
  private readonly tray: Tray;
  private readonly icons: { idle: Electron.NativeImage; connected: Electron.NativeImage };
  private pending: ReturnType<typeof setTimeout> | null = null;

  constructor(resourcesDir: string, files: TrayIcons, private readonly actions: TrayActions, private readonly state: () => TrayState) {
    const load = (name: string) => {
      const img = nativeImage.createFromPath(join(resourcesDir, name));
      if (files.template) img.setTemplateImage(true);
      return img;
    };
    this.icons = { idle: load(files.idle), connected: load(files.connected) };
    this.tray = new Tray(this.icons.idle);
    this.render();
  }

  /** Re-render soon (status events can come in bursts). */
  update(): void {
    if (this.pending) return;
    this.pending = setTimeout(() => { this.pending = null; this.render(); }, 500);
  }

  private render(): void {
    const s = this.state();
    const info = (label: string): MenuItemConstructorOptions => ({ label, enabled: false });
    const busy = s.bridge.current !== null || Object.values(s.bridge.queued).some((n) => n > 0);
    const update = updateMenuItem(s.update);
    const template: MenuItemConstructorOptions[] = [
      ...(update ? [{ label: update.label, click: () => (update.action === "install" ? this.actions.installUpdate() : this.actions.openSettings()) }, { type: "separator" } as const] : []),
      info(engineLine(s.engine)),
      ...bridgeLines(s.bridge).map(info),
      { type: "separator" },
      { label: "Open OptionLens", click: () => this.actions.open() },
      { label: "Settings…", click: () => this.actions.openSettings() },
      { type: "separator" },
      { label: s.bridge.enabled ? "Pause IBKR bridge" : "Resume IBKR bridge", click: () => this.actions.setBridgeEnabled(!s.bridge.enabled) },
      { label: "Cancel bridge work", enabled: busy, click: () => this.actions.cancelBridgeWork() },
      { type: "separator" },
      ...(s.loginItem === "unsupported" ? [] : [{
        label: s.loginItem === "requires-approval" ? "Launch at login (allow in System Settings → Login Items)" : "Launch at login",
        type: "checkbox", checked: s.loginItem !== "disabled", click: (item) => this.actions.setLaunchAtLogin(item.checked) } satisfies MenuItemConstructorOptions]),
      { label: "Quit OptionLens", accelerator: "CommandOrControl+Q", click: () => this.actions.quit() },
    ];
    this.tray.setContextMenu(Menu.buildFromTemplate(template));
    this.tray.setToolTip(trayTooltip(s.engine, s.bridge));
    this.tray.setImage(s.bridge.tws === "connected" ? this.icons.connected : this.icons.idle);
  }
}
