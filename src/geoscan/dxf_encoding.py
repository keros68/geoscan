from __future__ import annotations

import re
from pathlib import Path


DXF_UNICODE_ESCAPE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def decode_dxf_unicode_escapes_for_mapgis(text: str) -> str:
    def replace_match(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    decoded = DXF_UNICODE_ESCAPE.sub(replace_match, text)
    return decoded.replace(r"\~", " ")


def read_dxf_text(path: Path) -> str:
    """Read a DXF with the encoding-fallback chain MapGIS files show up in."""
    for encoding in ("utf-8", "gbk", "cp1252", "latin1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def make_dxf_mapgis_chinese_compatible(path: Path) -> None:
    text = read_dxf_text(path)
    text = decode_dxf_unicode_escapes_for_mapgis(text)
    text = text.replace("ANSI_1252", "ANSI_936")
    path.write_text(text, encoding="gbk", errors="replace", newline="")
