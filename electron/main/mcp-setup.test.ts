import { describe, expect, it } from "vitest";
import { mcpArgs, mcpPythonArgs, mcpSetup, type McpSetupInputs } from "./mcp-setup";
import { linuxPlatform } from "./platform/linux";
import { posixPython, windowsPython } from "./platform/python-layout";
import type { PlatformDeps } from "./platform/types";

const base: McpSetupInputs = {
  packaged: true, platform: "darwin", resourcesPath: "/Applications/OptionLens.app/Contents/Resources", launch: posixPython.mcpLaunch,
  devPython: "/repo/engine/.venv/bin/python", dataDir: "/Users/me/Library/Application Support/OptionLens/data",
  defaultDataDir: "/Users/me/Library/Application Support/OptionLens/data", exists: () => true,
};

describe("mcpSetup", () => {
  it("macOS: the launcher inside the app bundle", () => {
    expect(mcpSetup(base)).toEqual({ command: "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp", args: [],
      launcher: "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp", launcherFound: true, platform: "darwin" });
  });
  it("Windows: the .cmd through cmd /c (MCP clients spawn without a shell)", () => {
    const s = mcpSetup({ ...base, platform: "win32", resourcesPath: "/P/OptionLens/resources", launch: windowsPython.mcpLaunch });
    expect([s.command, ...s.args]).toEqual(["cmd", "/c", "/P/OptionLens/resources/mcp/optionlens-mcp.cmd"]);
    expect(s.platform).toBe("win32");
  });
  it("Linux AppImage: the AppImage itself with --mcp; an unpacked build: the launcher", () => {
    const deps = (env: Record<string, string>): PlatformDeps => ({ app: {} as PlatformDeps["app"], argv: [], env, execPath: "/opt/OptionLens/optionlens", packaged: true, home: "/home/me", files: { read: () => null, write: () => undefined, remove: () => undefined }, run: async () => "" });
    const appImage = linuxPlatform(deps({ APPIMAGE: "/home/me/Applications/OptionLens.AppImage" }));
    expect(mcpSetup({ ...base, platform: "linux", resourcesPath: "/tmp/.mount_x/resources", launch: appImage.mcpLaunch })).toMatchObject({
      command: "/home/me/Applications/OptionLens.AppImage", args: ["--no-sandbox", "--ozone-platform=headless", "--mcp"], platform: "linux" });
    const unpacked = linuxPlatform(deps({}));
    expect(mcpSetup({ ...base, platform: "linux", resourcesPath: "/opt/OptionLens/resources", launch: unpacked.mcpLaunch }).command).toBe("/opt/OptionLens/resources/mcp/optionlens-mcp");
  });
  it("development: the engine's virtualenv Python; a non-default data folder is passed on", () => {
    const s = mcpSetup({ ...base, packaged: false, dataDir: "/tmp/other" });
    expect(s).toEqual({ command: "/repo/engine/.venv/bin/python", args: ["-m", "optionlens.mcp", "--data-dir", "/tmp/other"], launcher: null, launcherFound: true, platform: "darwin" });
  });
  it("says when the launcher is missing", () => {
    expect(mcpSetup({ ...base, exists: () => false }).launcherFound).toBe(false);
  });
});

describe("--mcp", () => {
  it("is recognised with the arguments after it", () => {
    expect(mcpArgs(["/x/OptionLens", "--mcp", "--check"])).toEqual(["--check"]);
    expect(mcpArgs(["/x/OptionLens", "--opened-at-login"])).toBeNull();
    expect(mcpPythonArgs(["--check"])).toEqual(["-I", "-B", "-m", "optionlens.mcp", "--check"]);
  });
});
