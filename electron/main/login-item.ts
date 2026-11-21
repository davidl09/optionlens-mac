// "Launch at login" as the OS actually has it (platform/*: macOS login item, Windows Run key, Linux XDG
// autostart entry). The stored setting alone does not say whether OptionLens really starts at login: the
// user may have turned it off in System Settings, Task Manager or the desktop's startup settings.
import type { LoginItemStatus } from "../shared/api";

/** The launch-at-login setting implied by the OS state (null: keep the stored one). */
export function launchAtLoginFromOs(status: LoginItemStatus): boolean | null {
  return status === "unsupported" ? null : status !== "disabled";
}
