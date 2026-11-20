// One-time data import: copy an existing optionlens.duckdb (and its WAL, if any) into the engine's data
// dir. The current database, if present, is kept as a timestamped backup. Call with the engine stopped.
import { copyFileSync, existsSync, mkdirSync, openSync, readSync, closeSync, renameSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";

export const DB_FILE = "optionlens.duckdb";

/** DuckDB files start with an 8-byte checksum followed by the magic "DUCK" (bytes 8..11). */
export function looksLikeDuckDb(path: string): boolean {
  try {
    if (!statSync(path).isFile()) return false;
    const fd = openSync(path, "r");
    try {
      const buf = Buffer.alloc(12);
      return readSync(fd, buf, 0, 12, 0) === 12 && buf.subarray(8, 12).toString("latin1") === "DUCK";
    } finally { closeSync(fd); }
  } catch { return false; }
}

export interface ImportOutcome { target: string; backup: string | null }

export function importDatabase(source: string, dataDir: string, now = new Date()): ImportOutcome {
  if (!looksLikeDuckDb(source)) throw new Error("That file is not a DuckDB database.");
  mkdirSync(dataDir, { recursive: true });
  const target = join(dataDir, DB_FILE);
  if (source === target) throw new Error("That is already the app's database.");
  let backup: string | null = null;
  if (existsSync(target)) {
    backup = `${target}.bak-${now.toISOString().replace(/[:.]/g, "-")}`;
    renameSync(target, backup);
    if (existsSync(`${target}.wal`)) renameSync(`${target}.wal`, `${backup}.wal`);
  }
  const tmp = `${target}.importing`;
  copyFileSync(source, tmp);
  renameSync(tmp, target);
  rmSync(`${target}.wal`, { force: true });
  if (existsSync(`${source}.wal`)) copyFileSync(`${source}.wal`, `${target}.wal`);
  return { target, backup };
}
