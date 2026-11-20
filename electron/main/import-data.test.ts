import { existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { DB_FILE, importDatabase, looksLikeDuckDb } from "./import-data";

const duck = (body: string) => Buffer.concat([Buffer.alloc(8, 1), Buffer.from("DUCK"), Buffer.from(body)]);

describe("importDatabase", () => {
  it("copies the database and WAL, keeping the previous one as a backup", () => {
    const dir = mkdtempSync(join(tmpdir(), "ol-import-"));
    const src = join(dir, "old.duckdb");
    writeFileSync(src, duck("new"));
    writeFileSync(`${src}.wal`, "wal");
    const data = join(dir, "data");
    importDatabase(src, data);   // first import: nothing to back up
    writeFileSync(src, duck("newer"));
    const out = importDatabase(src, data, new Date("2026-11-20T12:00:00Z"));
    expect(readFileSync(join(data, DB_FILE)).subarray(12).toString()).toBe("newer");
    expect(readFileSync(join(data, `${DB_FILE}.wal`), "utf8")).toBe("wal");
    expect(out.backup).toContain("bak-2026-11-20T12-00-00-000Z");
    expect(existsSync(out.backup!)).toBe(true);
    expect(readdirSync(data).some((f) => f.endsWith(".importing"))).toBe(false);
  });

  it("refuses files that are not DuckDB databases", () => {
    const dir = mkdtempSync(join(tmpdir(), "ol-import-"));
    const src = join(dir, "notes.txt");
    writeFileSync(src, "hello world, definitely not duckdb");
    expect(looksLikeDuckDb(src)).toBe(false);
    expect(() => importDatabase(src, join(dir, "data"))).toThrow(/not a DuckDB/);
    expect(looksLikeDuckDb(join(dir, "missing"))).toBe(false);
  });
});
