"""Small JSON state files in DATA_DIR, written atomically (temp file in the same directory, fsync, rename): a crash
or power loss leaves either the old or the new content, never a torn file."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any | None:
    """The file's JSON value, or None when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, value: Any, mode: int = 0o644) -> None:
    """Replace `path` with `value` as JSON. The file is created with `mode` (POSIX; on Windows the user profile's
    default ACL applies) and the parent directory is created when missing. Raises OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
