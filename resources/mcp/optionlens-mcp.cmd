@echo off
rem OptionLens MCP server (stdio) for AI agents, Windows: runs the app's bundled Python with the engine's MCP
rem server (docs/INTEGRATION.md section 10). OptionLens must be running with Settings, AI agents (MCP) turned on.
rem Usage: claude mcp add optionlens -- cmd /c "C:\path\to\optionlens-mcp.cmd"
rem -I: no PYTHON* variables or user site-packages; -B: never write bytecode into the app.
"%~dp0..\python\python.exe" -I -B -m optionlens.mcp %*
