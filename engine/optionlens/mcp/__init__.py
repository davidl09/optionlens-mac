"""OptionLens for AI agents: a local stdio MCP server (`python -m optionlens.mcp`) over the running engine's API
(docs/INTEGRATION.md §10).

- catalog: the tools, generated from the engine's OpenAPI schema plus a thin curated layer (pure; the engine's auth
  uses it too, to decide what the MCP key may call);
- shaping: compact, paginated output and the table state of the current view (pure);
- conventions: units and data provenance, for agents (a resource and the `describe_units` tool);
- connection: the connection file and HTTP calls to the running engine;
- server: the MCP SDK wiring (tools, resources, resource templates).

Only `server` imports the MCP SDK, so the engine itself never needs it at runtime.
"""
