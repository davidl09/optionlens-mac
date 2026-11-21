// Renames and removals that survive a briefly busy file. Windows cannot rename or delete a file that any
// process has open, and right after the engine exits an antivirus scan or the search indexer may still hold
// its database for a moment (EBUSY / EPERM / EACCES). Elsewhere these errors are rare and the retry is harmless.
import { rename as fsRename, rm } from "node:fs/promises";

const BUSY = new Set(["EBUSY", "EPERM", "EACCES"]);

export interface RetryOptions {
  attempts?: number;
  delayMs?: number;
  rename?: (from: string, to: string) => Promise<void>;
  sleep?: (ms: number) => Promise<void>;
}

/** fs.rename, retried while the file is busy (default: 20 attempts, 250 ms apart: about 5 s). */
export async function renameRetrying(from: string, to: string, o: RetryOptions = {}): Promise<void> {
  const attempts = o.attempts ?? 20;
  const rename = o.rename ?? fsRename;
  const sleep = o.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  for (let i = 1; ; i++) {
    try {
      await rename(from, to);
      return;
    } catch (e) {
      if (i >= attempts || !BUSY.has((e as NodeJS.ErrnoException).code ?? "")) throw e;
      await sleep(o.delayMs ?? 250);
    }
  }
}

/** fs.rm({ force: true }) with Node's own retries for busy files (about 5 s). */
export function removeRetrying(path: string): Promise<void> {
  return rm(path, { force: true, recursive: true, maxRetries: 20, retryDelay: 250 });
}
