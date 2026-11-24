// AI agents (MCP, INTEGRATION.md §10) on the main-process side: the command an MCP client runs to start OptionLens's MCP
// server (shown in Settings → AI agents), and `OptionLens --mcp`, which runs that server over this process's stdio
// (the AppImage's stable path; its mount point changes on every run). The server itself is the engine package's
// `python -m optionlens.mcp`; agent access (the MCP key) is the engine's (/api/mcp).
import type { McpLaunch, McpSetup } from "../shared/api";
import { MCP_ARG } from "./platform/types";

export interface McpSetupInputs {
  packaged: boolean;
  platform: NodeJS.Platform;
  resourcesPath: string;
  launch: (resourcesPath: string) => McpLaunch;
  /** The engine's Python in development (engine/.venv). */
  devPython: string;
  dataDir: string;
  /** Where the MCP server looks without --data-dir (the app's default data folder). */
  defaultDataDir: string;
  exists: (path: string) => boolean;
}

/** The arguments after `--mcp` when this launch is an MCP server, else null. */
export function mcpArgs(argv: string[]): string[] | null {
  const i = argv.indexOf(MCP_ARG);
  return i < 0 ? null : argv.slice(i + 1);
}

/** The Python arguments of the MCP server (-I: no PYTHON* variables or user site-packages; -B: the app stays sealed). */
export function mcpPythonArgs(extra: string[]): string[] {
  return ["-I", "-B", "-m", "optionlens.mcp", ...extra];
}

export function mcpSetup(i: McpSetupInputs): McpSetup {
  const dataDir = i.dataDir === i.defaultDataDir ? [] : ["--data-dir", i.dataDir];
  const platform = i.platform === "darwin" || i.platform === "win32" ? i.platform : "linux";
  if (!i.packaged) {
    return { command: i.devPython, args: ["-m", "optionlens.mcp", ...dataDir], launcher: null, launcherFound: i.exists(i.devPython), platform };
  }
  const l = i.launch(i.resourcesPath);
  return { command: l.command, args: [...l.args, ...dataDir], launcher: l.launcher, launcherFound: i.exists(l.launcher), platform };
}
