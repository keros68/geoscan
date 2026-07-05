"""Build the lightweight engine-update asset: ``engine-<version>-rt<runtime>.zip``.

The zip holds the PUBLIC ``geoscan`` package (code + the section_bootstrap
resource) with ``geoscan/`` at the zip root, so a client extracts it over
``<install>/_internal/engine/`` to update the code without touching the ~100 MB
runtime. Upload it alongside ``GeoScanSetup.exe`` on the GitHub release.

Run from anywhere:  python release/build_engine_zip.py
Prints the output path + size; exits non-zero on any problem.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "src" / "geoscan"

# Local-only private tier — never shipped. Keep in step with .git/info/exclude
# and PRIVATE_MODULES in packaging/GeoScan.spec.
PRIVATE = {
    "native_direct", "wl_from_scratch", "wt_w60_derived", "wt_from_seed",
    "mapgis_binary", "mapgis_wl", "mapgis_wt", "mapgis_wp", "native_format_lab",
    "wt_native_diagnostics", "mapgis67_diagnostics",
}


def _engine_version() -> str:
    namespace: dict[str, object] = {}
    exec((PKG / "__init__.py").read_text(encoding="utf-8"), namespace)  # noqa: S102
    return str(namespace["__version__"])


def _runtime_version() -> str:
    return (REPO / "packaging" / "runtime_version.txt").read_text(encoding="utf-8").strip()


def main() -> int:
    version = _engine_version()
    runtime = _runtime_version()
    out_dir = REPO / "dist"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"engine-{version}-rt{runtime}.zip"
    if out.exists():
        out.unlink()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for py in sorted(PKG.glob("*.py")):
            if py.stem in PRIVATE:
                continue
            zf.write(py, f"geoscan/{py.name}")
        bootstrap = PKG / "section_bootstrap" / "SECTION_BOOTSTRAP.WT"
        if bootstrap.is_file():
            zf.write(bootstrap, "geoscan/section_bootstrap/SECTION_BOOTSTRAP.WT")

    print(f"{out}  ({out.stat().st_size / 1024:.0f} KB)  engine={version} runtime={runtime}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
