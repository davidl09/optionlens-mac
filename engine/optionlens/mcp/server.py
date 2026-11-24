"""The OptionLens MCP server (stdio) — `python -m optionlens.mcp` (docs/INTEGRATION.md §10).

Tools come from the catalog (the engine's OpenAPI schema + the curated layer), built in this process from the same
engine package the app runs, so they list even while the app is closed; every call goes to the running engine with
the MCP key (connection.py). Resources: the current view, the engine status, the data conventions and per-symbol
summaries (a resource template)."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import anyio
import jsonschema
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from . import catalog, shaping
from .catalog import ToolDef
from .connection import Engine, EngineError, app_data_dir
from .conventions import CONVENTIONS

log = logging.getLogger("optionlens.mcp")
SERVER_NAME = "optionlens"
INSTRUCTIONS = ("OptionLens: options and stock analytics on the user's computer (read-only market data; no trading). "
                "Start with get_current_view to see what the user is looking at; read describe_units before "
                "interpreting numbers (percentages and IV are fractions; option prices are per share). Reports: "
                "report_list, then report_run. Long arrays are paginated (limit / offset; `_page` says what was cut).")
DESCRIBE_UNITS = types.Tool(
    name="describe_units", description="Units and data-provenance conventions of OptionLens data (fractions vs "
    "percent, per-share vs per-contract dollars, greeks, source / delayed / model / computed / after-hours flags). "
    "Read once before interpreting numbers.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                                      open_world_hint=False))
RESOURCES = [
    types.Resource(uri="optionlens://current-view", name="current-view", mime_type="application/json",
                   description="What the user is looking at now, with the data the page shows (get_current_view)."),
    types.Resource(uri="optionlens://status", name="status", mime_type="application/json",
                   description="Engine and data-collection status (system_status)."),
    types.Resource(uri="optionlens://conventions", name="conventions", mime_type="text/markdown",
                   description="Units and data provenance (describe_units)."),
]
SYMBOL_TEMPLATE = types.ResourceTemplate(uri_template="optionlens://symbol/{symbol}/summary", name="symbol-summary",
                                         mime_type="application/json",
                                         description="One symbol at a glance (symbol_summary).")


def engine_tools() -> list[ToolDef]:
    """The catalog of this engine package (its own OpenAPI schema: no running engine needed)."""
    from ..api.app import create_app
    return catalog.build(create_app().openapi())


def _annotations(t: ToolDef) -> types.ToolAnnotations:
    return types.ToolAnnotations(read_only_hint=t.read_only, destructive_hint=t.destructive,
                                 idempotent_hint=t.idempotent, open_world_hint=False)


class OptionLensTools:
    """Tool calls and resource reads over the running engine (no MCP transport here: tests call it directly)."""

    def __init__(self, engine: Engine, tools: list[ToolDef]):
        self.engine, self.tools = engine, {t.name: t for t in tools}
        self.matcher = catalog.Matcher(tools)
        self._validators = {t.name: jsonschema.Draft202012Validator(t.input_schema) for t in tools}

    def listed(self) -> list[types.Tool]:
        """The tools, without those of a gate the user has not allowed (the portfolio)."""
        return [types.Tool(name=t.name, description=t.description, input_schema=t.input_schema,
                           annotations=_annotations(t)) for t in self.tools.values()
                if self.engine.allows(t.gate)] + [DESCRIBE_UNITS]

    async def call(self, name: str, args: dict[str, Any], client: str | None = None) -> Any:
        """A tool's answer (JSON-able, already shaped for the agent). EngineError / ValueError for the agent."""
        if name == DESCRIBE_UNITS.name:
            return CONVENTIONS
        t = self.tools.get(name)
        if t is None:
            raise ValueError(f"unknown tool {name!r}")
        if not self.engine.allows(t.gate):
            raise EngineError(catalog.GATES[t.gate])   # type: ignore[index]
        errors = sorted(self._validators[name].iter_errors(args), key=lambda e: list(e.path))
        if errors:
            raise ValueError("; ".join(f"{'.'.join(map(str, e.path)) or 'arguments'}: {e.message}" for e in errors[:5]))
        if t.post == "current_view":
            return await self.current_view(args, client)
        path, query, body = t.request(args)
        result = await self.engine.call(t.method, path, query, body, client=client)
        if t.post == "report_list":
            try:
                return shaping.report_list(result, args)
            except KeyError as e:
                raise ValueError(e.args[0]) from None
        return shaping.shape(result, args) if t.paged else result

    # ------------------------------------------------------------------ the current view
    def arguments(self, t: ToolDef, path: str) -> dict[str, Any]:
        """The tool arguments of a request path the page made (path parameters, query; report params)."""
        parts = urlsplit(path)
        out: dict[str, Any] = {}
        template, actual = t.path.split("/"), parts.path.split("/")
        for seg, val in zip(template, actual):
            if seg.startswith("{") and seg.endswith("}"):
                out[seg[1:-1]] = unquote(val)
        for k, v in parse_qsl(parts.query, keep_blank_values=False):
            if t.params_object and k not in t.query_params:
                out.setdefault(t.params_object, {})[k] = v
            else:
                out[k] = v
        return out

    async def current_view(self, args: dict[str, Any], client: str | None) -> Any:
        got = await self.engine.call("GET", "/api/view", client=client)
        view = got.get("view") if isinstance(got, dict) else None
        if not view:
            return {"view": None, "note": "The app has not reported a view yet (is its window open?)."}
        data = []
        for req in view.get("data", []):
            method, path = req.get("method", "GET"), req.get("path", "")
            t = self.matcher.match(method, urlsplit(path).path)
            entry: dict[str, Any] = {"request": f"{method} {path}"}
            if t is None or not t.read_only:
                data.append({**entry, "error": "not available to AI agents"})
                continue
            if not self.engine.allows(t.gate):
                data.append({**entry, "error": catalog.GATES[t.gate]})   # type: ignore[index]
                continue
            entry["tool"] = t.name
            entry["arguments"] = {**self.arguments(t, path), **(req.get("body") or {})}
            for k in ("table", "rows"):
                if req.get(k):
                    entry[k] = req[k]
            try:
                result = await self.engine.call(method, path, body=req.get("body"), client=client)
            except EngineError as e:
                data.append({**entry, "error": str(e)})
                continue
            if t.post == "report_list":            # e.g. the dashboard's report catalog: the compact list
                result = shaping.report_list(result, {})
            entry["result"] = self.shape_view(result, req, args)
            data.append(entry)
        meta = {k: v for k, v in view.items() if k != "data"}
        return {"view": meta, "reported_at": got.get("reported_at"), "age_s": got.get("age_s"), "data": data}

    @staticmethod
    def shape_view(result: Any, req: dict, args: dict[str, Any]) -> Any:
        """A page's data as the user sees it: table rows filtered and sorted like the screen, the visible rows and
        columns by default (the agent's limit / offset / columns override them)."""
        limit, offset, cols = shaping.page_args(args)
        table, rows = req.get("table"), req.get("rows")
        if isinstance(result, dict) and isinstance(result.get("rows"), list):
            if table:
                result = {**result, "rows": shaping.apply_table(result["rows"], table)}
                hidden = set(table.get("hidden") or ())
                if cols is None and hidden and isinstance(result.get("columns"), list):
                    cols = [c["key"] for c in result["columns"] if isinstance(c, dict) and c.get("key") not in hidden]
            if rows and "offset" not in args:
                offset = rows.get("offset", 0)
            if rows and "limit" not in args:
                limit = max(1, min(catalog.PAGE_DEFAULT, rows.get("limit", catalog.PAGE_DEFAULT)))
        if cols:
            result = shaping.select_columns(result, cols)
        return shaping.paginate(result, offset, limit)

    # ------------------------------------------------------------------ resources
    async def read(self, uri: str, client: str | None = None) -> tuple[str, str]:
        """(mime type, text) of a resource. ValueError for an unknown URI."""
        if uri == "optionlens://conventions":
            return "text/markdown", CONVENTIONS
        if uri == "optionlens://current-view":
            return "application/json", shaping.dumps(await self.call("get_current_view", {}, client))
        if uri == "optionlens://status":
            return "application/json", shaping.dumps(await self.call("system_status", {}, client))
        prefix, suffix = "optionlens://symbol/", "/summary"
        if uri.startswith(prefix) and uri.endswith(suffix) and len(uri) > len(prefix) + len(suffix):
            sym = unquote(uri[len(prefix):-len(suffix)])
            return "application/json", shaping.dumps(await self.call("symbol_summary", {"sym": sym}, client))
        raise ValueError(f"unknown resource {uri!r}")


def _client_name(ctx: Any) -> str | None:
    """The MCP client's name/version from its initialize request (shown in the app's Settings), if it sent one."""
    try:
        info = ctx.session.client_params.client_info
        return f"{info.name}/{info.version}" if info.version else info.name
    except AttributeError:
        return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else shaping.dumps(value)


def build_server(impl: OptionLensTools, version: str = "") -> Server:
    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=impl.listed())

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            value = await impl.call(params.name, dict(params.arguments or {}), _client_name(ctx))
        except (EngineError, ValueError) as e:
            return types.CallToolResult(content=[types.TextContent(text=str(e))], is_error=True)
        return types.CallToolResult(content=[types.TextContent(text=_text(value))])

    async def list_resources(ctx, params) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=RESOURCES)

    async def list_templates(ctx, params) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult(resource_templates=[SYMBOL_TEMPLATE])

    async def read_resource(ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        uri = str(params.uri)
        try:
            mime, text = await impl.read(uri, _client_name(ctx))
        except (EngineError, ValueError) as e:
            raise MCPError(types.INVALID_PARAMS if isinstance(e, ValueError) else types.INTERNAL_ERROR, str(e)) from None
        return types.ReadResourceResult(contents=[types.TextResourceContents(uri=uri, mime_type=mime, text=text)])

    return Server(SERVER_NAME, version=version, title="OptionLens", instructions=INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool, on_list_resources=list_resources,
                  on_list_resource_templates=list_templates, on_read_resource=read_resource)


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("optionlens")
    except PackageNotFoundError:
        return ""


async def _serve(data_dir: Path) -> None:
    engine = Engine(data_dir)
    try:
        server = build_server(OptionLensTools(engine, engine_tools()), _version())
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await engine.aclose()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="optionlens-mcp", description="OptionLens MCP server (stdio). The OptionLens "
                                "app must be running with Settings → AI agents (MCP) turned on.")
    p.add_argument("--data-dir", type=Path, help="the app's data folder (default: OPTIONLENS_DATA_DIR, else the app's "
                                                  "per-OS folder)")
    p.add_argument("--check", action="store_true", help="build the tool catalog, print a summary and exit")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    data_dir = a.data_dir or (Path(os.environ["OPTIONLENS_DATA_DIR"]) if os.environ.get("OPTIONLENS_DATA_DIR")
                              else app_data_dir())
    if a.check:
        tools = engine_tools()
        writes = sum(not t.read_only for t in tools)
        print(f"optionlens-mcp {_version()}: {len(tools) + 1} tools ({writes} write), data folder {data_dir}")
        return 0
    anyio.run(_serve, data_dir)
    return 0
