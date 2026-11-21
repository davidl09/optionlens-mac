import { describe, expect, it, vi } from "vitest";
import { renameRetrying } from "./fs-retry";

const busy = (code: string) => Object.assign(new Error(code), { code });

describe("renameRetrying", () => {
  it("retries while Windows reports the file busy, then succeeds", async () => {
    const rename = vi.fn().mockRejectedValueOnce(busy("EBUSY")).mockRejectedValueOnce(busy("EPERM")).mockResolvedValueOnce(undefined);
    const sleep = vi.fn(async () => undefined);
    await renameRetrying("a", "b", { rename, sleep, delayMs: 7 });
    expect(rename).toHaveBeenCalledTimes(3);
    expect(sleep).toHaveBeenCalledWith(7);
  });

  it("gives up after the last attempt and fails at once on other errors", async () => {
    const sleep = async () => undefined;
    await expect(renameRetrying("a", "b", { rename: async () => { throw busy("EACCES"); }, sleep, attempts: 3 })).rejects.toThrow("EACCES");
    const missing = vi.fn(async () => { throw busy("ENOENT"); });
    await expect(renameRetrying("a", "b", { rename: missing, sleep })).rejects.toThrow("ENOENT");
    expect(missing).toHaveBeenCalledTimes(1);
  });
});
