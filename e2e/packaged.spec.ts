// Packaged-app smoke test: launches the built app (electron-builder `--dir` output, bundled Python engine)
// and checks that the renderer (app://optionlens) really loads data from the loopback engine. This is
// what Chromium's Local Network Access checks can break, and what the mocked suites cannot see.
// Skipped unless PACKAGED_APP is set, e.g. (Linux, headless):
//   PACKAGED_APP=release/linux-unpacked/optionlens-mac xvfb-run -a npx playwright test e2e/packaged.spec.ts
import { _electron as electron, expect, test } from "@playwright/test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const APP = process.env.PACKAGED_APP;

test.skip(!APP, "set PACKAGED_APP to the packaged executable to run this test");
test.setTimeout(180_000);

test("the packaged renderer loads data from the bundled engine", async () => {
  const home = mkdtempSync(join(tmpdir(), "optionlens-packaged-"));
  // Linux: userData = $XDG_CONFIG_HOME/OptionLens, so the test never touches the user's data.
  const app = await electron.launch({
    executablePath: APP!,
    args: ["--no-sandbox", ...(process.env.PACKAGED_APP_ARGS ?? "").split(" ").filter(Boolean)],
    env: { ...process.env, XDG_CONFIG_HOME: join(home, "config"), HOME: home } as Record<string, string>,
    timeout: 60_000,
  });
  try {
    const page = await app.firstWindow();
    const failed: string[] = [];
    page.on("requestfailed", (r) => { if (r.url().startsWith("http://127.0.0.1")) failed.push(`${r.url()}: ${r.failure()?.errorText}`); });
    page.on("console", (m) => { if (m.type() === "error") failed.push(`console: ${m.text()}`); });
    const ok = await page.waitForResponse((r) => /^http:\/\/127\.0\.0\.1:\d+\/api\//.test(r.url()) && r.status() === 200, { timeout: 150_000 });
    expect(ok.ok()).toBe(true);
    expect(failed.filter((f) => /local network|loopback|ERR_BLOCKED|ERR_ACCESS_DENIED|Permission/i.test(f))).toEqual([]);
  } finally {
    await app.close().catch(() => undefined);
    rmSync(home, { recursive: true, force: true });
  }
});
