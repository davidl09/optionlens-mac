// Renderer permission policy: everything is denied except Chromium's Local Network Access permissions for
// the app's own origin — the renderer (app://optionlens) must reach the engine on 127.0.0.1, which
// Chromium's Local Network Access checks treat as a request into the local network.
import { APP_HOST, APP_SCHEME } from "./protocol";

/** Chromium / Electron names of the Local Network Access permissions (Electron 45: all three exist). */
export const LOCAL_NETWORK_PERMISSIONS: ReadonlySet<string> = new Set(["local-network-access", "loopback-network", "local-network"]);

/** Whether `permission` may be granted to a frame of `origin` (a URL or an origin). */
export function allowPermission(permission: string, origin: string | null | undefined): boolean {
  if (!LOCAL_NETWORK_PERMISSIONS.has(permission) || !origin) return false;
  try {
    const u = new URL(origin);   // URL.origin is "null" for custom schemes: compare scheme and host
    return u.protocol === `${APP_SCHEME}:` && u.host === APP_HOST;
  } catch { return false; }
}
