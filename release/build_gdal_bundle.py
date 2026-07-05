"""Assemble a minimal, self-contained GDAL bundle for offline DXF export.

The frozen app shells out to ``ogr2ogr`` to write DXF. To work on machines with
NO QGIS, we ship a ``gdal/`` folder next to the exe holding ``ogr2ogr.exe`` + its
full DLL dependency closure + ``gdal-data`` (the GDAL_DATA dir with header.dxf).
PROJ grid data (~800 MB) is deliberately NOT bundled — the pixel-unit DXF route
needs no datum grids, and the code tolerates a missing PROJ_LIB.

This computes ogr2ogr's DLL closure with ``objdump`` (from mingw/binutils, on the
maintainer's PATH) so the bundle carries exactly what ogr2ogr loads and nothing
else (~120 MB, vs ~420 MB for the whole QGIS bin).

Usage:
    python release/build_gdal_bundle.py "D:\\Qgis"

Output: packaging/gdal_bundle/  (git-ignored). build_clean.ps1 / the release
build then copy it into dist/GeoScan/gdal/.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "packaging" / "gdal_bundle"


def _closure(bin_dir: Path) -> list[str]:
    """Files in bin_dir reachable from ogr2ogr.exe via PE imports (recursive)."""
    present = {f.lower(): f for f in os.listdir(bin_dir)}
    seen: set[str] = set()

    def imports(path: Path) -> list[str]:
        try:
            out = subprocess.run(
                ["objdump", "-p", str(path)], capture_output=True, text=True, errors="replace"
            ).stdout
        except FileNotFoundError as exc:  # objdump missing
            raise SystemExit("需要 objdump（mingw/binutils）来计算 DLL 闭包，请安装或加入 PATH。") from exc
        return re.findall(r"DLL Name:\s*(\S+)", out)

    def walk(name: str) -> None:
        low = name.lower()
        if low in seen:
            return
        seen.add(low)
        real = present.get(low)
        if real is None:  # a Windows system DLL, not shipped
            return
        for dep in imports(bin_dir / real):
            walk(dep)

    walk("ogr2ogr.exe")
    return sorted(present[l] for l in seen if l in present)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: python release/build_gdal_bundle.py <QGIS 安装目录，如 D:\\Qgis>")
        return 2
    qgis = Path(argv[1])
    bin_dir = qgis / "bin"
    gdal_data = qgis / "apps" / "gdal" / "share" / "gdal"
    if not (bin_dir / "ogr2ogr.exe").is_file():
        print(f"未找到 {bin_dir / 'ogr2ogr.exe'}"); return 1
    if not (gdal_data / "header.dxf").is_file():
        print(f"未找到 GDAL_DATA（缺 header.dxf）: {gdal_data}"); return 1

    files = _closure(bin_dir)
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    for name in files:
        shutil.copy2(bin_dir / name, OUT / name)
    shutil.copytree(gdal_data, OUT / "gdal-data")

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"packaging/gdal_bundle 组装完成：{len(files)} 个 DLL/exe + gdal-data，共 {total/1024/1024:.0f} MB")
    print("下一步：release 构建会把它复制到 dist/GeoScan/gdal/。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
