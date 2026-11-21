// Test fixtures: a GitHub release JSON for the update tests.
export const REPO = "davidl09/optionlens-mac";
export const HEX = "a".repeat(64);
export const asset = (name: string, over: Record<string, unknown> = {}) => ({
  name, size: 100, digest: `sha256:${HEX}`, browser_download_url: `https://github.com/${REPO}/releases/download/v0.1.3/${name}`, ...over,
});
export const releaseJson = (over: Record<string, unknown> = {}) => ({
  tag_name: "v0.1.3", body: "Fixes.\n<script>alert(1)</script>", html_url: `https://github.com/${REPO}/releases/tag/v0.1.3`,
  published_at: "2026-11-21T20:00:00Z", draft: false, prerelease: false,
  assets: [asset("OptionLens-0.1.3-arm64.zip"), asset("OptionLens-0.1.3-x64.zip", { digest: `sha256:${"b".repeat(64)}` })], ...over,
});

