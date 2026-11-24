// Setup snippets for AI-agent clients (Settings → AI agents, INTEGRATION.md §10), from the command the main process
// resolved for this computer (McpSetup: the launcher in the app, `cmd /c` on Windows, the AppImage with --mcp).
import type { McpSetup } from "../../electron/shared/api";

export const SERVER_NAME = "optionlens";
export const MCP_WARNING = "Agents with this access can read all your OptionLens data and edit watchlists/alerts/presets. No trading is possible.";

/** One shell word: always quoted when it is the program (a path), otherwise only when it needs it. */
function word(s: string, windows: boolean, program = false): string {
  if (!program && /^[A-Za-z0-9_/.:=+@%,-]+$/.test(s)) return s;
  return windows ? `"${s.replace(/"/g, '""')}"` : `"${s.replace(/(["\\$`])/g, "\\$1")}"`;
}

export function commandLine(setup: Pick<McpSetup, "command" | "args" | "platform">): string {
  const win = setup.platform === "win32";
  // `cmd /c "<launcher>"`: cmd is a plain word, the launcher path the program
  const isCmd = win && setup.command.toLowerCase() === "cmd";
  return [isCmd ? "cmd" : word(setup.command, win, true), ...setup.args.map((a, i) => word(a, win, isCmd && i === 1))].join(" ");
}

export interface McpSnippets { claudeCode: string; claudeDesktop: string; generic: string; desktopConfigPath: string | null }

export function mcpSnippets(setup: McpSetup): McpSnippets {
  const line = commandLine(setup);
  return {
    claudeCode: `claude mcp add ${SERVER_NAME} -- ${line}`,
    claudeDesktop: JSON.stringify({ mcpServers: { [SERVER_NAME]: { command: setup.command, args: setup.args } } }, null, 2),
    generic: line,
    desktopConfigPath: setup.platform === "darwin" ? "~/Library/Application Support/Claude/claude_desktop_config.json"
      : setup.platform === "win32" ? "%APPDATA%\\Claude\\claude_desktop_config.json" : null,
  };
}
