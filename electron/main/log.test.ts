import { existsSync, mkdtempSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { createLogger } from "./log";

describe("createLogger", () => {
  it("rotates the log file by size and keeps the last 3", () => {
    vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const file = join(mkdtempSync(join(tmpdir(), "ol-log-")), "main.log");
    const log = createLogger(file, { maxBytes: 1000, keep: 3 });
    for (let i = 0; i < 100; i++) log.info(`line ${i} ${"x".repeat(80)}`);
    expect(statSync(file).size).toBeLessThanOrEqual(1000);
    for (const n of [1, 2, 3]) expect(statSync(`${file}.${n}`).size).toBeLessThanOrEqual(1000);
    expect(existsSync(`${file}.4`)).toBe(false);
    expect(readFileSync(file, "utf8")).toContain("line 99");
    // a new logger on an existing file continues its size count
    const again = createLogger(file, { maxBytes: 1000, keep: 3 });
    for (let i = 0; i < 20; i++) again.warn("y".repeat(100));
    expect(statSync(file).size).toBeLessThanOrEqual(1000);
    vi.restoreAllMocks();
  });
});
