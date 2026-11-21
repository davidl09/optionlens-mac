#!/usr/bin/env python3
"""Fail when a Mach-O file under DIR needs a newer macOS than MAX (e.g. 12.0): wheels built for macOS 14
would make the app crash at import time on macOS 12/13, below the app's minimumSystemVersion.

Reads LC_BUILD_VERSION (minos) and LC_VERSION_MIN_MACOSX of every Mach-O (thin or universal) file.
Usage: check-macos-minos.py DIR [MAX]   (MAX defaults to mac.minimumSystemVersion in electron-builder.yml)
"""
from __future__ import annotations

import os
import re
import struct
import sys

FAT_MAGICS = {0xCAFEBABE: False, 0xCAFEBABF: True}            # big-endian fat headers (value: 64-bit arch table)
THIN = {0xFEEDFACE: ("<", 28), 0xFEEDFACF: ("<", 32),        # thin magic (read little-endian): byte order, header size
        0xCEFAEDFE: (">", 28), 0xCFFAEDFE: (">", 32)}
LC_VERSION_MIN_MACOSX, LC_BUILD_VERSION, PLATFORM_MACOS = 0x24, 0x32, 1


def _version(v: int) -> tuple[int, int, int]:
    return v >> 16, (v >> 8) & 0xFF, v & 0xFF


def _thin_minos(data: bytes, off: int) -> list[tuple[int, int, int]]:
    magic = struct.unpack_from("<I", data, off)[0]
    if magic not in THIN:
        return []
    end, hdr = THIN[magic]
    ncmds = struct.unpack_from(end + "I", data, off + 16)[0]
    p, out = off + hdr, []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from(end + "II", data, p)
        if cmd == LC_BUILD_VERSION:
            platform, minos = struct.unpack_from(end + "II", data, p + 8)
            if platform == PLATFORM_MACOS:
                out.append(_version(minos))
        elif cmd == LC_VERSION_MIN_MACOSX:
            out.append(_version(struct.unpack_from(end + "I", data, p + 8)[0]))
        p += size
    return out


def macho_minos(path: str) -> list[tuple[int, int, int]] | None:
    """Minimum macOS versions of the file's slices; None when it is not a Mach-O file."""
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) < 8:
            return None
        be = struct.unpack(">I", head[:4])[0]
        le = struct.unpack("<I", head[:4])[0]
        if be in FAT_MAGICS:
            nfat = struct.unpack(">I", head[4:])[0]
            if nfat > 30:          # Java class files share the 0xCAFEBABE magic
                return None
            data = head + f.read()
            wide = FAT_MAGICS[be]
            out = []
            for i in range(nfat):
                if wide:
                    offset = struct.unpack_from(">Q", data, 8 + i * 32 + 8)[0]
                else:
                    offset = struct.unpack_from(">I", data, 8 + i * 20 + 8)[0]
                out += _thin_minos(data, offset)
            return out
        if le not in THIN:
            return None
        return _thin_minos(head + f.read(), 0)


def main(root: str, limit: str) -> int:
    want = tuple(int(x) for x in (limit.split(".") + ["0", "0"])[:3])
    checked, bad = 0, []
    for d, _, files in os.walk(root):
        for name in files:
            path = os.path.join(d, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            mins = macho_minos(path)
            if mins is None:
                continue
            checked += 1
            worst = max(mins, default=(0, 0, 0))
            if worst > want:
                bad.append((".".join(map(str, worst)), os.path.relpath(path, root)))
    for v, p in sorted(bad):
        print(f"needs macOS {v}: {p}", file=sys.stderr)
    print(f"{checked} Mach-O files checked under {root}; {len(bad)} need a macOS newer than {limit}")
    if checked == 0:
        print("no Mach-O files found (wrong directory?)", file=sys.stderr)
        return 1
    return 1 if bad else 0


def app_minimum() -> str:
    """mac.minimumSystemVersion from electron-builder.yml (the one place the app's minimum macOS is set)."""
    yml = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "electron-builder.yml")
    with open(yml, encoding="utf-8") as f:
        m = re.search(r'^\s*minimumSystemVersion:\s*"?([0-9.]+)"?', f.read(), re.M)
    if not m:
        sys.exit("no minimumSystemVersion in electron-builder.yml")
    return m.group(1)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else app_minimum()))
