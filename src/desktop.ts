// The desktop (Electron) API exposed by the preload, or null when the UI runs in a plain browser
// (vite dev / the mocked e2e suite), where /api is same-origin and there is no bridge.
import type { DesktopApi } from "../electron/shared/api";

declare global {
  interface Window { optionlens?: DesktopApi }
}

export const desktop: DesktopApi | null = typeof window !== "undefined" && window.optionlens ? window.optionlens : null;
