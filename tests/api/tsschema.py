"""Tiny extractor of TypeScript interface shapes from the app's src/api/types.ts (contract tests).

Schema nodes: ("obj", {field: (schema, optional)}), ("arr", item), ("leaf",). Handles inline object
types, arrays (`T[]`), unions with null (`T | null`), intersections (`A & { ... }`), parentheses and
references to other interfaces/aliases; anything else (primitives, Record<>, literals) is a leaf.
"""
from __future__ import annotations

import os
import re
from pathlib import Path



def find_types_ts() -> Path | None:
    """The app's API contract: $OPTIONLENS_TYPES_TS, else src/api/types.ts of the enclosing optionlens-mac
    checkout (engine embedded in the app repo) or of a sibling optionlens-mac checkout."""
    if env := os.environ.get("OPTIONLENS_TYPES_TS"):
        return Path(env)
    for d in Path(__file__).resolve().parents:
        for c in (d / "src" / "api" / "types.ts", d / "optionlens-mac" / "src" / "api" / "types.ts"):
            if c.is_file():
                return c
    return None


TYPES_TS = find_types_ts()
_OPEN, _CLOSE = "{([<", "})]>"


def _strip_comments(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


def _split_top(s: str, sep: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _block(src: str, start: int) -> tuple[str, int]:
    """Body of the brace block opening at src[start] and the index after it."""
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1:i], i + 1
    raise ValueError("unbalanced braces")


def load_types() -> "Types":
    """Types of the app contract; skips the calling test module when no optionlens-mac checkout is found."""
    if TYPES_TS is None or not TYPES_TS.is_file():
        import pytest
        pytest.skip("API contract src/api/types.ts not found (no optionlens-mac checkout next to or around the "
                    "engine; set OPTIONLENS_TYPES_TS)", allow_module_level=True)
    return Types(TYPES_TS)


class Types:
    def __init__(self, path: Path):
        src = _strip_comments(path.read_text())
        self.raw: dict[str, str] = {}
        for m in re.finditer(r"export interface (\w+)\s*\{", src):
            body, _ = _block(src, m.end() - 1)
            self.raw[m.group(1)] = "{" + body + "}"
        for m in re.finditer(r"export type (\w+)\s*=\s*([^;]+);", src):
            self.raw.setdefault(m.group(1), m.group(2).strip())

    def interface_fields(self, name: str) -> set[str]:
        return set(self.schema(name)[1])

    def schema(self, name: str) -> tuple:
        return self.parse(self.raw[name])

    def parse(self, t: str) -> tuple:
        t = t.strip()
        parts = [p for p in _split_top(t, "|") if p not in ("null", "undefined")]
        if len(parts) != 1:
            return ("leaf",)
        t = parts[0]
        inter = _split_top(t, "&")
        if len(inter) > 1:
            fields: dict = {}
            for p in inter:
                s = self.parse(p)
                if s[0] != "obj":
                    return ("leaf",)
                fields.update(s[1])
            return ("obj", fields)
        if t.endswith("[]"):
            return ("arr", self.parse(t[:-2]))
        if t.startswith("(") and t.endswith(")"):
            return self.parse(t[1:-1])
        if t.startswith("{"):
            return ("obj", self._fields(t[1:-1]))
        if t in self.raw:
            return self.parse(self.raw[t])
        return ("leaf",)

    def _fields(self, body: str) -> dict:
        out = {}
        for decl in (d for part in _split_top(body, ";") for d in _split_top(part, "\n")):
            m = re.match(r"^(\w+)(\?)?\s*:\s*(.+)$", decl.strip(), re.S)
            if m:
                out[m.group(1)] = (self.parse(m.group(3).rstrip(",")), bool(m.group(2)))
        return out


def mismatches(value, schema: tuple, path: str = "$", max_items: int = 50) -> list[str]:
    """Paths where JSON `value` does not have exactly the schema's keys (optional keys may be absent)."""
    kind = schema[0]
    if value is None or kind == "leaf":
        return []
    if kind == "arr":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        return [e for i, v in enumerate(value[:max_items]) for e in mismatches(v, schema[1], f"{path}[{i}]")]
    if not isinstance(value, dict):
        return [f"{path}: expected object"]
    fields = schema[1]
    required = {k for k, (_, opt) in fields.items() if not opt}
    errs = []
    if missing := required - set(value):
        errs.append(f"{path}: missing {sorted(missing)}")
    if extra := set(value) - set(fields):
        errs.append(f"{path}: unexpected {sorted(extra)}")
    for k, (s, _) in fields.items():
        if k in value:
            errs.extend(mismatches(value[k], s, f"{path}.{k}"))
    return errs
