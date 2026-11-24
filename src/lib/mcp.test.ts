import { describe, expect, it } from "vitest";
import type { McpSetup } from "../../electron/shared/api";
import { commandLine, mcpSnippets } from "./mcp";

const mac: McpSetup = { command: "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp", args: [], launcher: "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp", launcherFound: true, platform: "darwin" };

describe("MCP setup snippets", () => {
  it("Claude Code on macOS: the launcher path, quoted", () => {
    expect(mcpSnippets(mac).claudeCode).toBe('claude mcp add optionlens -- "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp"');
  });
  it("Claude Desktop JSON config and the generic command", () => {
    const s = mcpSnippets(mac);
    expect(JSON.parse(s.claudeDesktop)).toEqual({ mcpServers: { optionlens: { command: mac.command, args: [] } } });
    expect(s.generic).toBe('"/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp"');
    expect(s.desktopConfigPath).toContain("Library/Application Support/Claude");
  });
  it("Windows: cmd /c with the .cmd path quoted (spaces in the user name)", () => {
    const win: McpSetup = { command: "cmd", args: ["/c", "C:\\Users\\Jo Doe\\AppData\\Local\\Programs\\OptionLens\\resources\\mcp\\optionlens-mcp.cmd"], launcher: "x", launcherFound: true, platform: "win32" };
    expect(commandLine(win)).toBe('cmd /c "C:\\Users\\Jo Doe\\AppData\\Local\\Programs\\OptionLens\\resources\\mcp\\optionlens-mcp.cmd"');
    expect(JSON.parse(mcpSnippets(win).claudeDesktop).mcpServers.optionlens).toEqual({ command: "cmd", args: win.args });
  });
  it("Linux AppImage: the AppImage with --mcp; POSIX quoting escapes $ and quotes", () => {
    expect(commandLine({ command: "/home/me/Apps/Option$Lens \"x\".AppImage", args: ["--mcp"], platform: "linux" })).toBe('"/home/me/Apps/Option\\$Lens \\"x\\".AppImage" --mcp');
    expect(mcpSnippets({ ...mac, platform: "linux" }).desktopConfigPath).toBeNull();
  });
});
