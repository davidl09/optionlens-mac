import { existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { copyWithProgress, DB_FILE, importDatabase, looksLikeDuckDb, type ImportProgress } from "./import-data";

const duck = (body: string) => Buffer.concat([Buffer.alloc(8, 1), Buffer.from("DUCK"), Buffer.from(body)]);

describe("importDatabase", () => {
  it("copies the database and WAL, keeping the previous one as a backup", async () => {
    const dir = mkdtempSync(join(tmpdir(), "ol-import-"));
    const src = join(dir, "old.duckdb");
    writeFileSync(src, duck("new"));
    writeFileSync(`${src}.wal`, "wal");
    const data = join(dir, "data");
    await importDatabase(src, data);   // first import: nothing to back up
    writeFileSync(src, duck("newer"));
    const progress: ImportProgress[] = [];
    const out = await importDatabase(src, data, { now: new Date("2026-11-20T12:00:00Z"), onProgress: (p) => progress.push(p) });
    expect(readFileSync(join(data, DB_FILE)).subarray(12).toString()).toBe("newer");
    expect(readFileSync(join(data, `${DB_FILE}.wal`), "utf8")).toBe("wal");
    expect(out.backup).toContain("bak-2026-11-20T12-00-00-000Z");
    expect(existsSync(out.backup!)).toBe(true);
    expect(readdirSync(data).some((f) => f.includes(".importing"))).toBe(false);
    expect(progress.at(-1)).toEqual({ copied: 17, total: 17 });
  });

  it("refuses files that are not DuckDB databases", async () => {
    const dir = mkdtempSync(join(tmpdir(), "ol-import-"));
    const src = join(dir, "notes.txt");
    writeFileSync(src, "hello world, definitely not duckdb");
    expect(looksLikeDuckDb(src)).toBe(false);
    await expect(importDatabase(src, join(dir, "data"))).rejects.toThrow(/not a DuckDB/);
    expect(looksLikeDuckDb(join(dir, "missing"))).toBe(false);
  });

  it("copies without blocking the event loop and reports progress for large files", async () => {
    const dir = mkdtempSync(join(tmpdir(), "ol-import-"));
    const src = join(dir, "big.duckdb");
    writeFileSync(src, Buffer.alloc(150 * 1024 * 1024, 7));
    const progress: ImportProgress[] = [];
    let ticks = 0;
    const timer = setInterval(() => { ticks += 1; }, 1);
    await copyWithProgress(src, join(dir, "copy.duckdb"), (p) => progress.push(p));
    clearInterval(timer);
    expect(progress.at(-1)).toEqual({ copied: 150 * 1024 * 1024, total: 150 * 1024 * 1024 });
    expect(readFileSync(join(dir, "copy.duckdb")).length).toBe(150 * 1024 * 1024);
    expect(ticks).toBeGreaterThan(0);
  });
});
