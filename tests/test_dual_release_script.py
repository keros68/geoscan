from __future__ import annotations

import subprocess
from pathlib import Path


def test_dual_release_script_dry_run_orders_latest_json_last():
    script = Path(__file__).resolve().parents[1] / "release" / "publish_dual_release.ps1"
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Version",
            "9.9.9",
            "-Runtime",
            "1",
            "-Notes",
            "dry run",
            "-DryRun",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    output = result.stdout
    clean_build_pos = output.index("release/build_clean.ps1")
    engine_build_pos = output.index("release/build_engine_zip.py")
    assert clean_build_pos < engine_build_pos
    assert "release/build_engine_zip.py" in output
    assert "gh release" in output
    assert "release/build_update_manifest.py" in output
    payload_pos = output.index("Upload mirror assets before latest.json")
    latest_pos = output.index("Upload mirror latest.json last")
    assert payload_pos < latest_pos
    assert "https://aidraw.cv/geoscan-updates/GeoScanSetup.exe" in output
