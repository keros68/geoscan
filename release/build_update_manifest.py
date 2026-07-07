"""Build an upload-ready static update mirror for GeoScan.

Output layout:

    dist/update_mirror/
      latest.json
      releases/v<version>/
        GeoScanSetup.exe
        engine-<version>-rt<runtime>.zip
        sha256.txt

The folder can be synced to the web root used by ``updater.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "src" / "geoscan"
DEFAULT_BASE_URL = "https://aidraw.cv/geoscan-updates"
GITHUB_REPO = "keros68/geoscan"
INSTALLER_ASSET_NAME = "GeoScanSetup.exe"


def _engine_version() -> str:
    namespace: dict[str, object] = {}
    exec((PKG / "__init__.py").read_text(encoding="utf-8"), namespace)  # noqa: S102
    return str(namespace["__version__"])


def _runtime_version() -> str:
    return (REPO / "packaging" / "runtime_version.txt").read_text(encoding="utf-8").strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset(name: str, path: Path, url: str) -> dict[str, object]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "name": name,
        "url": url,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build_manifest(
    *,
    version: str,
    runtime: str,
    base_url: str,
    installer: Path,
    engine: Path,
    notes: str = "",
) -> dict[str, object]:
    version = version.lstrip("vV")
    tag = f"v{version}"
    base = base_url.rstrip("/")
    release_base = f"{base}/releases/{tag}"
    return {
        "version": version,
        "tag": tag,
        "runtime": str(runtime),
        "notes": notes,
        "github": f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}",
        "latest_installer_url": f"{base}/{INSTALLER_ASSET_NAME}",
        "assets": [
            _asset(INSTALLER_ASSET_NAME, installer, f"{release_base}/{INSTALLER_ASSET_NAME}"),
            _asset(Path(engine).name, engine, f"{release_base}/{Path(engine).name}"),
        ],
    }


def stage_update_mirror(
    *,
    version: str,
    runtime: str,
    base_url: str,
    installer: Path,
    engine: Path,
    out_dir: Path,
    notes: str = "",
) -> Path:
    manifest = build_manifest(
        version=version,
        runtime=runtime,
        base_url=base_url,
        installer=installer,
        engine=engine,
        notes=notes,
    )
    tag = str(manifest["tag"])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    release_dir = out_dir / "releases" / tag
    release_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(installer, out_dir / INSTALLER_ASSET_NAME)
    shutil.copy2(installer, release_dir / INSTALLER_ASSET_NAME)
    shutil.copy2(engine, release_dir / Path(engine).name)

    sha_lines = [
        f"{asset['sha256']}  {asset['name']}"
        for asset in manifest["assets"]
        if isinstance(asset, dict)
    ]
    (release_dir / "sha256.txt").write_text("\n".join(sha_lines) + "\n", encoding="utf-8")

    manifest_path = out_dir / "latest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _parse_args() -> argparse.Namespace:
    version = _engine_version()
    runtime = _runtime_version()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=version)
    parser.add_argument("--runtime", default=runtime)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--notes", default="")
    parser.add_argument("--installer", type=Path, default=REPO / "dist" / "installer" / INSTALLER_ASSET_NAME)
    parser.add_argument("--engine", type=Path, default=REPO / "dist" / f"engine-{version}-rt{runtime}.zip")
    parser.add_argument("--out-dir", type=Path, default=REPO / "dist" / "update_mirror")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    manifest = stage_update_mirror(
        version=args.version,
        runtime=args.runtime,
        base_url=args.base_url,
        installer=args.installer,
        engine=args.engine,
        out_dir=args.out_dir,
        notes=args.notes,
    )
    print(f"wrote {manifest}")
    print(f"sync {args.out_dir} to the web root behind {args.base_url.rstrip('/')}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
