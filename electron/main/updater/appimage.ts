// Checks a downloaded AppImage before it replaces the installed one (the Linux counterpart of the macOS
// bundle checks): an ELF file with the type-2 AppImage magic, whose embedded desktop entry names
// OptionLens and the release's version (electron-builder writes X-AppImage-Version). The entry is read
// with the AppImage's own `--appimage-extract '*.desktop'` (no FUSE needed), after the SHA-256 check.
import { closeSync, openSync, readSync } from "node:fs";
import { readdir, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { desktopEntryKeys } from "../desktop-entry";
import { PRODUCT_NAME } from "../app-id";

/** Runs a program (absolute path, no shell) in `cwd`; resolves with stdout, rejects on a non-zero exit. */
export type ExecIn = (file: string, args: string[], cwd: string) => Promise<string>;

/** ELF magic, then "AI" + 0x02 at offset 8 (AppImage type 2). */
export function hasAppImageMagic(path: string): boolean {
  const fd = openSync(path, "r");
  try {
    const b = Buffer.alloc(11);
    return readSync(fd, b, 0, 11, 0) === 11 && b.subarray(0, 4).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46])) && b.subarray(8, 11).equals(Buffer.from([0x41, 0x49, 0x02]));
  } finally { closeSync(fd); }
}

/** null when the desktop entry is OptionLens at `expectedVersion`, else why not. */
export function appImageMismatch(entry: Map<string, string>, expectedVersion: string): string | null {
  if (entry.get("Name") !== PRODUCT_NAME) return `The downloaded AppImage is "${entry.get("Name") ?? "(unnamed)"}", not ${PRODUCT_NAME}.`;
  const v = entry.get("X-AppImage-Version");
  if (v !== expectedVersion) return `The downloaded AppImage is version ${v || "(none)"}, not ${expectedVersion}.`;
  return null;
}

/** Checks the (executable) AppImage at `path`; `workDir` is an empty scratch folder. Throws when it does not pass. */
export async function checkAppImage(exec: ExecIn, path: string, workDir: string, expectedVersion: string): Promise<void> {
  if (!hasAppImageMagic(path)) throw new Error("The download is not an AppImage.");
  await exec(path, ["--appimage-extract", "*.desktop"], workDir);
  const root = join(workDir, "squashfs-root");
  try {
    const entries = (await readdir(root)).filter((n) => n.endsWith(".desktop"));
    if (entries.length !== 1) throw new Error(`The AppImage must hold one desktop entry (found ${entries.length}).`);
    const mismatch = appImageMismatch(desktopEntryKeys(await readFile(join(root, entries[0]), "utf8")), expectedVersion);
    if (mismatch) throw new Error(mismatch);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}
