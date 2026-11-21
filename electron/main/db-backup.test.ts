import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { backupDatabaseOnVersionChange, backupName } from "./db-backup";

describe("database backup on a version change", () => {
  let root: string, data: string, backups: string;
  let last: string | null;
  const marker = { read: () => last, write: (v: string) => { last = v; } };
  const run = (version: string, iso: string) => backupDatabaseOnVersionChange({ dataDir: data, backupsDir: backups, currentVersion: version, marker, now: new Date(iso) });
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "ol-db backup-"));
    data = join(root, "data"); backups = join(root, "backups");
    mkdirSync(data);
    writeFileSync(join(data, "optionlens.duckdb"), "db-v1");
    last = "0.1.2";
  });
  afterEach(() => { rmSync(root, { recursive: true, force: true }); });

  it("names backups after the previous version and the time", () => {
    expect(backupName("0.1.2", new Date("2026-11-21T19:30:05.123Z"))).toBe("db-0.1.2-20261121T193005Z.duckdb");
    expect(backupName(null, new Date("2026-11-21T19:30:05Z"))).toBe("db-unknown-20261121T193005Z.duckdb");
    expect(backupName("../../x", new Date("2026-11-21T19:30:05Z"))).toBe("db-unknown-20261121T193005Z.duckdb");
  });

  it("copies the database (and WAL) once per new version, then records the version", async () => {
    writeFileSync(join(data, "optionlens.duckdb.wal"), "wal");
    const out = await run("0.1.3", "2026-11-21T19:30:00Z");
    expect(out).toBe(join(backups, "db-0.1.2-20261121T193000Z.duckdb"));
    expect(readFileSync(out!, "utf8")).toBe("db-v1");
    expect(readFileSync(`${out}.wal`, "utf8")).toBe("wal");
    expect(last).toBe("0.1.3");
    expect(await run("0.1.3", "2026-11-21T19:40:00Z")).toBeNull();          // same version: nothing
    expect(readdirSync(backups)).toHaveLength(2);
  });

  it("keeps the newest 2 backups", async () => {
    await run("0.1.3", "2026-11-21T10:00:00Z");
    await run("0.1.4", "2026-11-22T10:00:00Z");
    await run("0.1.5", "2026-11-23T10:00:00Z");
    expect(readdirSync(backups).sort()).toEqual(["db-0.1.3-20261122T100000Z.duckdb", "db-0.1.4-20261123T100000Z.duckdb"]);
  });

  it("first launch ever (no database): records the version, copies nothing", async () => {
    rmSync(join(data, "optionlens.duckdb"));
    last = null;
    expect(await run("0.1.3", "2026-11-21T10:00:00Z")).toBeNull();
    expect(existsSync(backups)).toBe(false);
    expect(last).toBe("0.1.3");
  });

  it("a failed copy leaves no partial file and does not record the version (tried again next launch)", async () => {
    mkdirSync(backups);
    writeFileSync(join(backups, "db-0.1.2-20261121T100000Z.duckdb.partial"), "");
    rmSync(join(data, "optionlens.duckdb"));
    mkdirSync(join(data, "optionlens.duckdb"));             // a folder: copying it fails
    await expect(run("0.1.3", "2026-11-21T10:00:00Z")).rejects.toThrow();
    expect(readdirSync(backups)).toEqual([]);
    expect(last).toBe("0.1.2");
  });
});
