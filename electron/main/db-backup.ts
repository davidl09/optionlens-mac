// Before the first engine start of a new app version, the database is copied to
// <backups>/db-<previous version>-<UTC time>.duckdb (with its WAL, if any), and the newest 2 such backups
// are kept: an update whose engine migrates the database, or a rollback to the previous app, never loses
// data. The copy is an APFS clone where possible (instant). The version that last ran is remembered.
import { existsSync } from "node:fs";
import { mkdir, readdir, rename, rm } from "node:fs/promises";
import { join } from "node:path";
import { copyWithProgress, DB_FILE } from "./import-data";

export const KEEP_DB_BACKUPS = 2;
const BACKUP_RE = /^db-[0-9A-Za-z.+-]+-(\d{8}T\d{6}Z)\.duckdb$/;

export interface VersionMarker { read(): string | null; write(version: string): void }

export interface DbBackupOptions {
  dataDir: string;
  backupsDir: string;
  currentVersion: string;
  marker: VersionMarker;
  now: Date;
  keep?: number;
}

export function backupName(previousVersion: string | null, now: Date): string {
  const v = previousVersion && /^[0-9A-Za-z.+-]{1,40}$/.test(previousVersion) ? previousVersion : "unknown";
  return `db-${v}-${now.toISOString().replace(/\.\d+Z$/, "Z").replace(/[-:]/g, "")}.duckdb`;
}

/** Deletes all but the newest `keep` database backups (by the time in their name). */
export async function pruneDbBackups(dir: string, keep: number): Promise<string[]> {
  const backups = (await readdir(dir)).map((n) => ({ n, m: BACKUP_RE.exec(n) })).filter((x) => x.m)
    .sort((a, b) => (a.m![1] < b.m![1] ? 1 : a.m![1] > b.m![1] ? -1 : 0)).map((x) => x.n);
  const drop = backups.slice(keep);
  for (const n of drop) { await rm(join(dir, n), { force: true }); await rm(join(dir, `${n}.wal`), { force: true }); }
  return drop;
}

/** Returns the backup's path, or null when the version did not change or there is no database yet. */
export async function backupDatabaseOnVersionChange(o: DbBackupOptions): Promise<string | null> {
  const previous = o.marker.read();
  if (previous === o.currentVersion) return null;
  const db = join(o.dataDir, DB_FILE);
  let target: string | null = null;
  if (existsSync(db)) {
    await mkdir(o.backupsDir, { recursive: true });
    target = join(o.backupsDir, backupName(previous, o.now));
    const tmp = `${target}.partial`;
    try {
      await copyWithProgress(db, tmp);
      if (existsSync(`${db}.wal`)) await copyWithProgress(`${db}.wal`, `${tmp}.wal`);
      if (existsSync(`${tmp}.wal`)) await rename(`${tmp}.wal`, `${target}.wal`);
      await rename(tmp, target);
    } catch (e) {
      await rm(tmp, { force: true });
      await rm(`${tmp}.wal`, { force: true });
      throw e;
    }
    await pruneDbBackups(o.backupsDir, o.keep ?? KEEP_DB_BACKUPS);
  }
  o.marker.write(o.currentVersion);   // only after a successful backup: a failed one is tried again next launch
  return target;
}
