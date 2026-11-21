import { describe, expect, it, vi } from "vitest";
import type { UpdateSettings, UpdateStatus } from "../../shared/api";
import { silentLogger } from "../log";
import { releaseJson, REPO } from "./fixtures.test-helper";
import { EMPTY_RECORD, type UpdateRecord } from "./record";
import { parseRelease, type Release } from "./release";
import { Updater, type UpdaterDeps } from "./updater";

const release = (version = "0.1.3", over: Record<string, unknown> = {}): Release =>
  parseRelease(releaseJson({ tag_name: `v${version}`, assets: [{ name: `OptionLens-${version}-arm64.zip`, size: 10, digest: `sha256:${"c".repeat(64)}`,
    browser_download_url: `https://github.com/${REPO}/releases/download/v${version}/OptionLens-${version}-arm64.zip` }], ...over }), REPO);

function harness(o: { settings?: Partial<UpdateSettings>; latest?: Release | Error; blocker?: string | null; record?: Partial<UpdateRecord>; location?: string | null } = {}) {
  let rec: UpdateRecord = { ...EMPTY_RECORD, ...o.record };
  let settings: UpdateSettings = { auto: false, source: REPO, ...o.settings };
  const statuses: UpdateStatus[] = [];
  const blocker = { value: o.blocker ?? null };
  const d = {
    currentVersion: "0.1.2", assetName: (v: string) => `OptionLens-${v}-arm64.zip`,
    settings: () => settings,
    latest: vi.fn(async () => { if (o.latest instanceof Error) throw o.latest; return o.latest ?? release(); }),
    download: vi.fn<UpdaterDeps["download"]>(async (spec) => { spec.onProgress?.(0.5); spec.onProgress?.(1); }),
    stage: vi.fn(async (_zip: string, stageDir: string) => `${stageDir}/OptionLens.app`),
    resetDir: vi.fn(async () => undefined),
    location: () => (o.location ? { ok: false as const, reason: o.location } : { ok: true as const, appPath: "/Applications/OptionLens.app" }),
    blocker: vi.fn(() => blocker.value),
    install: vi.fn(async () => undefined),
    records: { get: () => rec, set: (r: UpdateRecord) => { rec = r; } },
    notify: vi.fn(),
    onStatus: (s: UpdateStatus) => statuses.push(s),
    workDir: "/u/updates",
    now: () => new Date("2026-11-21T20:00:00Z"),
    log: silentLogger,
  } satisfies UpdaterDeps;
  const u = new Updater(d);
  return { u, d, statuses, blocker, rec: () => rec, setSettings: (s: Partial<UpdateSettings>) => { settings = { ...settings, ...s }; } };
}

describe("updater", () => {
  it("up to date: a release that is not newer (same, older, prerelease, draft) installs nothing", async () => {
    for (const r of [release("0.1.2"), release("0.1.1"), release("0.1.3", { prerelease: true }), release("0.2.0-beta.1"), release("0.1.3", { draft: true })]) {
      const h = harness({ latest: r, settings: { auto: true } });
      await h.u.check();
      expect(h.u.status()).toMatchObject({ phase: "up-to-date", currentVersion: "0.1.2" });
      expect(h.d.download).not.toHaveBeenCalled();
      expect(h.d.notify).not.toHaveBeenCalled();
    }
  });

  it("manual mode: tells the user once per version, then downloads, verifies, stages and installs on request", async () => {
    const h = harness();
    await h.u.check();
    expect(h.u.status()).toMatchObject({ phase: "available", latest: { version: "0.1.3", notes: expect.stringContaining("Fixes.") } });
    await h.u.check();
    expect(h.d.notify).toHaveBeenCalledTimes(1);
    expect(h.d.notify).toHaveBeenCalledWith("available", "0.1.3");
    expect(h.d.download).not.toHaveBeenCalled();

    expect(await h.u.installNow()).toEqual({ ok: true });
    expect(h.d.resetDir).toHaveBeenCalledWith("/u/updates/0.1.3");
    expect(h.d.download.mock.calls[0][0]).toMatchObject({
      url: `https://github.com/${REPO}/releases/download/v0.1.3/OptionLens-0.1.3-arm64.zip`, dest: "/u/updates/0.1.3/OptionLens-0.1.3-arm64.zip", sha256: "c".repeat(64), size: 10,
    });
    expect(h.d.stage).toHaveBeenCalledWith("/u/updates/0.1.3/OptionLens-0.1.3-arm64.zip", "/u/updates/0.1.3/stage", "0.1.3", "/Applications/OptionLens.app");
    expect(h.d.blocker).toHaveBeenCalledWith(true);
    expect(h.d.install).toHaveBeenCalledWith("/u/updates/0.1.3/stage/OptionLens.app", "/Applications/OptionLens.app", "0.1.3");
    expect(h.statuses.map((s) => s.phase)).toEqual(expect.arrayContaining(["checking", "available", "downloading", "ready", "installing"]));
    expect(h.statuses.filter((s) => s.phase === "downloading").map((s) => s.progress)).toEqual([0, 0.5, 1]);
  });

  it("automatic mode: downloads in the background, says it is ready, installs only when the policy allows", async () => {
    const h = harness({ settings: { auto: true }, blocker: "Waiting until the computer is idle to restart into the update." });
    await h.u.check();
    expect(h.u.status().phase).toBe("ready");
    expect(h.d.notify).toHaveBeenCalledWith("ready", "0.1.3");
    await h.u.tick();
    expect(h.d.blocker).toHaveBeenLastCalledWith(false);
    expect(h.d.install).not.toHaveBeenCalled();
    expect(h.u.status()).toMatchObject({ phase: "ready", blocked: expect.stringMatching(/idle/) });
    h.blocker.value = null;
    await h.u.tick();
    expect(h.d.install).toHaveBeenCalledTimes(1);
  });

  it("Restart now installs a ready update unless the import / closing-window rules block it", async () => {
    const h = harness({ settings: { auto: true }, blocker: "A data import is running; the update installs after it finishes." });
    await h.u.check();
    expect(await h.u.installNow()).toEqual({ ok: false, error: expect.stringMatching(/import/) });
    expect(h.d.install).not.toHaveBeenCalled();
    h.blocker.value = null;
    expect(await h.u.installNow()).toEqual({ ok: true });
    expect(h.d.install).toHaveBeenCalledTimes(1);
  });

  it("never downloads automatically a version that was rolled back; the user still can", async () => {
    const h = harness({ settings: { auto: true }, record: { skipped: ["0.1.3"] } });
    await h.u.check();
    expect(h.u.status()).toMatchObject({ phase: "available", skipped: true });
    expect(h.d.download).not.toHaveBeenCalled();
    expect(await h.u.installNow()).toEqual({ ok: true });
    expect(h.d.install).toHaveBeenCalled();
  });

  it("a translocated / read-only app is not downloaded into: it is asked to move first", async () => {
    const h = harness({ location: "Move OptionLens to the Applications folder first" });
    await h.u.check();
    expect(await h.u.installNow()).toEqual({ ok: false, error: expect.stringMatching(/Applications/) });
    expect(h.d.download).not.toHaveBeenCalled();
    expect(h.u.status()).toMatchObject({ phase: "available", blocked: expect.stringMatching(/Applications/) });
  });

  it("errors: failed check, missing digest, failed verification (retry works)", async () => {
    const failed = harness({ latest: new Error("GitHub's rate limit for update checks is used up.") });
    await failed.u.check();
    expect(failed.u.status()).toMatchObject({ phase: "error", error: expect.stringMatching(/rate limit/) });

    const noDigest = harness({ latest: parseRelease(releaseJson({ assets: [] }), REPO) });
    await noDigest.u.check();
    expect(await noDigest.u.installNow()).toMatchObject({ ok: false, error: expect.stringMatching(/has no OptionLens-0.1.3-arm64.zip/) });

    const bad = harness();
    bad.d.download.mockRejectedValueOnce(new Error("The download's SHA-256 (x) does not match the release (y); it was deleted."));
    await bad.u.check();
    expect(await bad.u.installNow()).toMatchObject({ ok: false, error: expect.stringMatching(/does not match/) });
    expect(bad.u.status()).toMatchObject({ phase: "error" });
    expect(bad.d.stage).not.toHaveBeenCalled();
    expect(await bad.u.installNow()).toEqual({ ok: true });
    expect(bad.d.install).toHaveBeenCalledTimes(1);
  });

  it("a new update source starts over; turning on automatic mode downloads a waiting update", async () => {
    const h = harness();
    await h.u.check();
    h.setSettings({ auto: true });
    await h.u.settingsChanged();
    expect(h.u.status().phase).toBe("ready");
    h.setSettings({ source: "someone/fork" });
    await h.u.settingsChanged();
    expect(h.d.latest).toHaveBeenLastCalledWith("someone/fork");
  });

  it("dismisses the notice", () => {
    const h = harness({ record: { notice: "rolled back" } });
    expect(h.u.status().notice).toBe("rolled back");
    h.u.dismissNotice();
    expect(h.u.status().notice).toBeNull();
    expect(h.rec().notice).toBeNull();
  });
});
