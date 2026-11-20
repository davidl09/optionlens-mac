// "Launch at login" as macOS actually has it. On macOS 13+ the login item is an SMAppService that the
// user can disable in System Settings → General → Login Items, or that waits for their approval; the
// stored setting alone does not say whether OptionLens really starts at login.
import type { LoginItemStatus } from "../shared/api";

/** What app.getLoginItemSettings() returns (status: macOS 13+ only). */
export interface LoginItemSettingsLike { openAtLogin: boolean; status?: string }

export function loginItemStatus(platform: NodeJS.Platform, s: LoginItemSettingsLike): LoginItemStatus {
  if (platform !== "darwin") return "unsupported";
  switch (s.status) {
    case "enabled": return "enabled";
    case "requires-approval": return "requires-approval";
    case "not-registered":
    case "not-found": return "disabled";
    default: return s.openAtLogin ? "enabled" : "disabled";   // macOS 12: no status
  }
}

/** The launch-at-login setting implied by the OS state (null: keep the stored one). */
export function launchAtLoginFromOs(status: LoginItemStatus): boolean | null {
  return status === "unsupported" ? null : status !== "disabled";
}
