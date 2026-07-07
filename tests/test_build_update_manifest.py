from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "release" / "build_update_manifest.py"
    spec = importlib.util.spec_from_file_location("build_update_manifest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_update_manifest = _load_module()


def test_build_manifest_uses_mirror_urls_and_sha256(tmp_path):
    installer = tmp_path / "GeoScanSetup.exe"
    engine = tmp_path / "engine-0.2.4-rt6.zip"
    installer.write_bytes(b"installer")
    engine.write_bytes(b"engine")

    manifest = build_update_manifest.build_manifest(
        version="0.2.4",
        runtime="6",
        base_url="https://aidraw.cv/geoscan-updates/",
        installer=installer,
        engine=engine,
        notes="mirror notes",
    )

    assert manifest["version"] == "0.2.4"
    assert manifest["tag"] == "v0.2.4"
    assert manifest["notes"] == "mirror notes"
    assert manifest["github"] == "https://github.com/keros68/geoscan/releases/tag/v0.2.4"
    assert manifest["runtime"] == "6"
    assert manifest["latest_installer_url"] == "https://aidraw.cv/geoscan-updates/GeoScanSetup.exe"
    assert manifest["assets"] == [
        {
            "name": "GeoScanSetup.exe",
            "url": "https://aidraw.cv/geoscan-updates/releases/v0.2.4/GeoScanSetup.exe",
            "size": len(b"installer"),
            "sha256": hashlib.sha256(b"installer").hexdigest(),
        },
        {
            "name": "engine-0.2.4-rt6.zip",
            "url": "https://aidraw.cv/geoscan-updates/releases/v0.2.4/engine-0.2.4-rt6.zip",
            "size": len(b"engine"),
            "sha256": hashlib.sha256(b"engine").hexdigest(),
        },
    ]


def test_stage_update_mirror_writes_upload_tree(tmp_path):
    installer = tmp_path / "GeoScanSetup.exe"
    engine = tmp_path / "engine-0.2.4-rt6.zip"
    installer.write_bytes(b"installer")
    engine.write_bytes(b"engine")
    out_dir = tmp_path / "mirror"

    manifest_path = build_update_manifest.stage_update_mirror(
        version="0.2.4",
        runtime="6",
        base_url="https://aidraw.cv/geoscan-updates",
        installer=installer,
        engine=engine,
        out_dir=out_dir,
        notes="mirror notes",
    )

    release_dir = out_dir / "releases" / "v0.2.4"
    assert manifest_path == out_dir / "latest.json"
    assert (out_dir / "GeoScanSetup.exe").read_bytes() == b"installer"
    assert (release_dir / "GeoScanSetup.exe").read_bytes() == b"installer"
    assert (release_dir / "engine-0.2.4-rt6.zip").read_bytes() == b"engine"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["assets"][0]["sha256"] in (release_dir / "sha256.txt").read_text(encoding="utf-8")
    assert manifest["assets"][1]["sha256"] in (release_dir / "sha256.txt").read_text(encoding="utf-8")
