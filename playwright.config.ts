import { existsSync } from "node:fs";
import { defineConfig } from "@playwright/test";

// Use a pre-installed Chromium when present, else the system Chrome (no `playwright install` needed).
const bundled = "/opt/pw-browsers/chromium";
const launch = existsSync(bundled) ? { launchOptions: { executablePath: bundled } } : { channel: "chrome" };

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 2,
  reporter: [["list"]],
  use: { baseURL: "http://127.0.0.1:4173", viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, trace: "off", ...launch },
  webServer: { command: "npx vite preview --host 127.0.0.1 --port 4173 --strictPort", url: "http://127.0.0.1:4173", reuseExistingServer: true, timeout: 30_000 },
});
