// Starts the POSIX helper (helper-script.ts) as a detached process in its own temp folder, so it outlives
// the app: the caller quits OptionLens right after, and the helper does the swap once the app has exited.
import { helperScript, type SwapPlan } from "./helper-script";

export interface SwapDeps {
  makeTempDir(): Promise<string>;
  writeFile(path: string, content: string, mode: number): Promise<void>;
  spawnDetached(file: string, args: string[]): void;
}

export async function launchSwap(plan: Omit<SwapPlan, "workDir" | "archive"> & { archive: string | null }, deps: SwapDeps): Promise<void> {
  const workDir = await deps.makeTempDir();
  // No archive (rollback): the app being replaced goes into the helper's temp folder and is discarded.
  const script = helperScript({ ...plan, archive: plan.archive ?? `${workDir}/discarded`, workDir });
  const file = `${workDir}/swap.sh`;
  await deps.writeFile(file, script, 0o700);
  deps.spawnDetached("/bin/bash", [file]);
}
