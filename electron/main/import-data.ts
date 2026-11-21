// One-time data import: copy an existing optionlens.duckdb (and its WAL, if any) into the engine's data
// dir. The copy is asynchronous (the main process stays responsive for a multi-GB file): an APFS clone
// where possible (instant), else a streamed copy that reports progress. The new file is complete before
// the current database, if present, is moved aside as a timestamped backup. Call with the engine stopped:
// DuckDB holds the file open (on Windows an open file cannot be renamed); renames retry while it is busy.
import { closeSync, constants, createReadStream, createWriteStream, existsSync, openSync, readSync, statSync } from "node:fs";
import { copyFile, mkdir, stat } from "node:fs/promises";
import { join } from "node:path";
import { Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import { removeRetrying as rm, renameRetrying as rename } from "./fs-retry";

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

export interface ImportProgress { copied: number; total: number }
export interface ImportOutcome { target: string; backup: string | null }

/** Copies src → dst without blocking: a copy-on-write clone if the file system can, else a streamed copy. */
export async function copyWithProgress(src: string, dst: string, onProgress: (p: ImportProgress) => void = () => undefined): Promise<void> {
  const total = (await stat(src)).size;
  try {
    await copyFile(src, dst, constants.COPYFILE_FICLONE_FORCE);
  } catch {
    let copied = 0;
    let reported = 0;
    const count = new Transform({
      transform(chunk: Buffer, _enc, done) {
        copied += chunk.length;
        if (copied - reported >= 64 * 1024 * 1024) { reported = copied; onProgress({ copied, total }); }
        done(null, chunk);
      },
    });
    await pipeline(createReadStream(src), count, createWriteStream(dst));
  }
  onProgress({ copied: total, total });
}

export async function importDatabase(source: string, dataDir: string, o: { now?: Date; onProgress?: (p: ImportProgress) => void } = {}): Promise<ImportOutcome> {
  if (!looksLikeDuckDb(source)) throw new Error("That file is not a DuckDB database.");
  await mkdir(dataDir, { recursive: true });
  const target = join(dataDir, DB_FILE);
  if (source === target) throw new Error("That is already the app's database.");
  const tmp = `${target}.importing`;
  try {
    await copyWithProgress(source, tmp, o.onProgress);
    if (existsSync(`${source}.wal`)) await copyFile(`${source}.wal`, `${tmp}.wal`);
  } catch (e) {
    await rm(tmp);
    await rm(`${tmp}.wal`);
    throw e;
  }
  let backup: string | null = null;
  if (existsSync(target)) {
    backup = `${target}.bak-${(o.now ?? new Date()).toISOString().replace(/[:.]/g, "-")}`;
    await rename(target, backup);
    if (existsSync(`${target}.wal`)) await rename(`${target}.wal`, `${backup}.wal`);
  }
  await rename(tmp, target);
  await rm(`${target}.wal`);
  if (existsSync(`${tmp}.wal`)) await rename(`${tmp}.wal`, `${target}.wal`);
  return { target, backup };
}
