import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { CONTENT_SECURITY_POLICY, resolveAsset, serveAsset } from "./protocol";

describe("app:// protocol", () => {
  const root = mkdtempSync(join(tmpdir(), "ol-dist-"));
  mkdirSync(join(root, "assets"));
  writeFileSync(join(root, "index.html"), "<html></html>");
  writeFileSync(join(root, "assets", "app.js"), "console.log(1)");

  it("maps routes to index.html and assets to files, refusing traversal", () => {
    expect(resolveAsset(root, "/")).toBe(join(root, "index.html"));
    expect(resolveAsset(root, "/s/AAPL/chain")).toBe(join(root, "index.html"));
    expect(resolveAsset(root, "/assets/app.js")).toBe(join(root, "assets", "app.js"));
    expect(resolveAsset(root, "/../../etc/passwd.txt")).toBeNull();
    expect(resolveAsset(root, "/%2e%2e/%2e%2e/etc/x.js")).toBeNull();
  });

  it("serves with CSP and MIME type; other hosts 404", async () => {
    const r = await serveAsset(root, "app://optionlens/assets/app.js");
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toMatch(/javascript/);
    expect(r.headers.get("content-security-policy")).toBe(CONTENT_SECURITY_POLICY);
    expect((await serveAsset(root, "app://optionlens/reports/iv-rankings")).headers.get("content-type")).toMatch(/html/);
    expect((await serveAsset(root, "app://evil/assets/app.js")).status).toBe(404);
    expect((await serveAsset(root, "app://optionlens/assets/missing.js")).status).toBe(404);
  });

  it("CSP only lets the renderer talk to the loopback engine", () => {
    expect(CONTENT_SECURITY_POLICY).toContain("connect-src http://127.0.0.1:*");
    expect(CONTENT_SECURITY_POLICY).toContain("script-src 'self'");
    expect(CONTENT_SECURITY_POLICY).not.toContain("unsafe-eval");
  });
});
