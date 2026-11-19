"""JSON encoding for API responses: NaN/inf -> null, dates/datetimes -> ISO strings, numpy -> Python."""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any

import numpy as np
from starlette.responses import JSONResponse


def clean(v: Any) -> Any:
    """Recursively make a payload JSON-safe (NaN/inf -> None)."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, np.generic):
        return clean(v.item())
    if isinstance(v, np.ndarray):
        return clean(v.tolist())
    return v


class ApiJSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(clean(content), allow_nan=False, separators=(",", ":")).encode()
