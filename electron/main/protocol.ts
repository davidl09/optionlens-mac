// The renderer is served from app://optionlens (privileged, standard, secure) so its origin matches the
// engine's CORS allowlist. Static files come from the Vite build; unknown extension-less paths fall back
// to index.html (client-side routes). Every response carries the CSP.
import { readFile } from "node:fs/promises";
import { extname, join, normalize, sep } from "node:path";

export const APP_SCHEME = "app";
export const APP_HOST = "optionlens";
export const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`;

/** connect-src allows the loopback engine on any port (it binds port 0, so the port changes on restart). */
export const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src http://127.0.0.1:*",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
  "object-src 'none'",
].join("; ");

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json", ".map": "application/json",
  ".woff2": "font/woff2", ".woff": "font/woff", ".txt": "text/plain; charset=utf-8",
};

/** File to serve for a request path, or null (outside root / unknown asset). */
export function resolveAsset(root: string, urlPath: string): string | null {
  let decoded: string;
  try { decoded = decodeURIComponent(urlPath); } catch { return null; }
  if (decoded.includes("\0")) return null;
  if (decoded === "/" || !extname(decoded)) return join(root, "index.html");
  const file = normalize(join(root, decoded));
  return file.startsWith(root.endsWith(sep) ? root : root + sep) ? file : null;
}

export async function serveAsset(root: string, requestUrl: string): Promise<Response> {
  const url = new URL(requestUrl);
  const headers = { "Content-Security-Policy": CONTENT_SECURITY_POLICY, "X-Content-Type-Options": "nosniff" };
  if (url.host !== APP_HOST) return new Response("not found", { status: 404, headers });
  const file = resolveAsset(root, url.pathname);
  if (!file) return new Response("not found", { status: 404, headers });
  try {
    const body = await readFile(file);
    return new Response(body, { status: 200, headers: { ...headers, "Content-Type": MIME[extname(file)] ?? "application/octet-stream" } });
  } catch {
    return new Response("not found", { status: 404, headers });
  }
}
