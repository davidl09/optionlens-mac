// Where python-build-standalone (the bundled engine, scripts/bundle-engine.sh) and a virtualenv keep the
// interpreter: bin/ on macOS and Linux, the folder itself (bundle) or Scripts\ (virtualenv) on Windows. Next to it,
// resources/mcp holds the MCP launcher that runs that Python with the engine's MCP server (INTEGRATION.md §10): a
// shell script on macOS and Linux, a .cmd on Windows (started through cmd /c: MCP clients spawn without a shell).
import { join } from "node:path";
import type { McpLaunch } from "../../shared/api";

export const posixPython = {
  bundledPython: (resources: string) => join(resources, "python", "bin", "python3"),
  venvPython: (venv: string) => join(venv, "bin", "python"),
  mcpLaunch: (resources: string): McpLaunch => {
    const launcher = join(resources, "mcp", "optionlens-mcp");
    return { command: launcher, args: [], launcher };
  },
};

export const windowsPython = {
  bundledPython: (resources: string) => join(resources, "python", "python.exe"),
  venvPython: (venv: string) => join(venv, "Scripts", "python.exe"),
  mcpLaunch: (resources: string): McpLaunch => {
    const launcher = join(resources, "mcp", "optionlens-mcp.cmd");
    return { command: "cmd", args: ["/c", launcher], launcher };
  },
};
