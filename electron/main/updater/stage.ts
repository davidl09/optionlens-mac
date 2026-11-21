// Unpacks a downloaded zip into an empty staging folder with ditto (as install.sh does) and checks the
// result before anything is replaced: exactly one OptionLens.app (a real folder, not a link), the expected
// bundle id and version in its Info.plist, and a valid signature (codesign --verify --deep --strict).
import { lstat, mkdir, readdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { APP_ID } from "../app-id";

/** Runs a program (absolute path, no shell) and resolves with its stdout; rejects on a non-zero exit. */
export type Exec = (file: string, args: string[]) => Promise<string>;

export const APP_BUNDLE = "OptionLens.app";
export const BUNDLE_ID = APP_ID;

export interface BundleInfo { bundleId: string; version: string }

/** null when the bundle is the expected app and version, else why not. */
export function bundleMismatch(info: BundleInfo, expectedVersion: string): string | null {
  if (info.bundleId !== BUNDLE_ID) return `The downloaded app has bundle id "${info.bundleId}", not ${BUNDLE_ID}.`;
  if (info.version !== expectedVersion) return `The downloaded app is version ${info.version || "(none)"}, not ${expectedVersion}.`;
  return null;
}

export async function readBundleInfo(exec: Exec, app: string): Promise<BundleInfo> {
  const plist = join(app, "Contents", "Info.plist");
  const value = async (key: string) => (await exec("/usr/libexec/PlistBuddy", ["-c", `Print :${key}`, plist])).trim();
  return { bundleId: await value("CFBundleIdentifier"), version: await value("CFBundleShortVersionString") };
}

/** Extracts `zip` into `stageDir` (emptied first) and returns the validated app's path; throws otherwise. */
export async function stageBundle(exec: Exec, zip: string, stageDir: string, expectedVersion: string): Promise<string> {
  await rm(stageDir, { recursive: true, force: true });
  await mkdir(stageDir, { recursive: true });
  await exec("/usr/bin/ditto", ["-x", "-k", zip, stageDir]);
  const entries = await readdir(stageDir);
  const app = join(stageDir, APP_BUNDLE);
  if (entries.length !== 1 || entries[0] !== APP_BUNDLE || !(await lstat(app)).isDirectory()) {
    throw new Error(`The update zip must contain only ${APP_BUNDLE} (found: ${entries.join(", ") || "nothing"}).`);
  }
  const mismatch = bundleMismatch(await readBundleInfo(exec, app), expectedVersion);
  if (mismatch) throw new Error(mismatch);
  try {
    await exec("/usr/bin/codesign", ["--verify", "--deep", "--strict", app]);
  } catch (e) {
    throw new Error(`The downloaded app's signature is invalid: ${(e as Error).message}`);
  }
  return app;
}
