"""/api/mcp (AI-agent access, the app's Settings only) and /api/view (the renderer's current view), docs/INTEGRATION.md
§10. The MCP key can call neither /api/mcp nor PUT /api/view (the MCP catalog excludes them); GET /api/view backs the
MCP tool `get_current_view`."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..services import mcp_access, view
from .json import ApiJSON

router = APIRouter(prefix="/api", tags=["agents"])


class McpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.get("/mcp")
async def mcp_status():
    """{enabled, connection_file, port, last_call, clients}: never the key."""
    return ApiJSON(mcp_access.instance().status())


@router.put("/mcp")
async def mcp_set(body: McpIn):
    """Turn agent access on (a new key when it was off) or off (key revoked, connection file removed)."""
    a = mcp_access.instance()
    try:
        return ApiJSON(a.enable() if body.enabled else a.disable())
    except OSError as e:
        raise HTTPException(500, detail=f"Could not write the MCP connection file: {e}") from e


@router.post("/mcp/regenerate")
async def mcp_regenerate():
    """A new key; the old one stops working at once (409 while access is off)."""
    try:
        return ApiJSON(mcp_access.instance().regenerate())
    except ValueError as e:
        raise HTTPException(409, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(500, detail=f"Could not write the MCP connection file: {e}") from e


# ----------------------------------------------------------------------------- current view
class SortIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=100)
    dir: Literal["asc", "desc"]


class FilterIn(BaseModel):
    """A table column filter (types.ts ColumnFilter, the same shape presets store)."""
    model_config = ConfigDict(extra="forbid")
    min: float | None = None
    max: float | None = None
    text: str | None = Field(default=None, max_length=200)
    values: list[str] | None = Field(default=None, max_length=500)


class TableIn(BaseModel):
    """The table state the user applied to a request's rows (client-side, like the UI's table)."""
    model_config = ConfigDict(extra="forbid")
    sort: SortIn | None = None
    filters: dict[str, FilterIn] = Field(default_factory=dict, max_length=100)
    hidden: list[str] = Field(default_factory=list, max_length=200)
    symbols: list[str] | None = Field(default=None, max_length=5000)   # "Watchlist only": rows of these symbols


class RowsIn(BaseModel):
    """The rows on screen: [offset, offset + limit) of `total` rows after the table state."""
    model_config = ConfigDict(extra="forbid")
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=5000)
    total: int = Field(ge=0)


class DataIn(BaseModel):
    """One engine request whose answer the page shows (path with its query string, exactly as the page sent it)."""
    model_config = ConfigDict(extra="forbid")
    method: Literal["GET", "POST"]
    path: str = Field(pattern=r"^/api/", max_length=4096)
    body: dict[str, Any] | None = None
    table: TableIn | None = None
    rows: RowsIn | None = None


Detail = str | int | float | bool | None


class ViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: str = Field(max_length=2048)            # pathname + search
    title: str | None = Field(default=None, max_length=300)
    symbol: str | None = Field(default=None, max_length=20)
    tab: str | None = Field(default=None, max_length=60)
    report_id: str | None = Field(default=None, max_length=100)
    details: dict[str, Detail] = Field(default_factory=dict, max_length=30)
    data: list[DataIn] = Field(default_factory=list, max_length=20)


@router.put("/view")
async def put_view(body: ViewIn):
    """The renderer's current view (on navigation and changes)."""
    view.report(body.model_dump(mode="json"))
    return ApiJSON({"ok": True})


@router.get("/view")
async def get_view():
    """{view, reported_at, age_s}: the view the renderer last reported (view null before the first)."""
    return ApiJSON(view.current())
