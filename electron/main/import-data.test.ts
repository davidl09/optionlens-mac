import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { copyWithProgress, DB_FILE, importDatabase, looksLikeDuckDb, type ImportProgress } from "./import-data";

const duck = (body: string) => Buffer.concat([Buffer.alloc(8, 1), Buffer.from("DUCK"), Buffer.from(body)]);

const dirs: string[] = [];
const tempDir = () => { const d = mkdtempSync(join(tmpdir(), "ol-import-")); dirs.push(d); return d; };
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }); });

describe("importDatabase", () => {
  it("copies the database and WAL, keeping the previous one as a backup", async () => {
    const dir = tempDir();
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
    const dir = tempDir();
    const src = join(dir, "notes.txt");
    writeFileSync(src, "hello world, definitely not duckdb");
    expect(looksLikeDuckDb(src)).toBe(false);
    await expect(importDatabase(src, join(dir, "data"))).rejects.toThrow(/not a DuckDB/);
    expect(looksLikeDuckDb(join(dir, "missing"))).toBe(false);
  });

  // Timing-independent: no wall-clock tick counting and a file small enough for slow CI disks (an 80 MB copy timed out
  // on a macOS runner); the progress interval is scaled down instead of the file up.
  it("copies without blocking the event loop and reports progress for large files", async () => {
    const dir = tempDir();
    const src = join(dir, "big.duckdb");
    const size = 6 * 1024 * 1024 + 123;
    writeFileSync(src, Buffer.alloc(size, 7));
    const progress: ImportProgress[] = [];
    const copy = copyWithProgress(src, join(dir, "copy.duckdb"), (p) => progress.push(p), 1024 * 1024);
    expect(progress).toEqual([]);                       // returned before copying anything: the caller's loop runs on
    await copy;
    expect(progress.at(-1)).toEqual({ copied: size, total: size });
    expect(progress.every((p, i) => i === 0 || p.copied >= progress[i - 1].copied)).toBe(true);
    // a streamed copy (no copy-on-write clone here) reports along the way; a clone (APFS) is instant: final only
    expect(progress.length === 1 || progress.length >= 6).toBe(true);
    expect(statSync(join(dir, "copy.duckdb")).size).toBe(size);
  }, 30_000);
});
