"""Chinese-install-path regression: ogr2ogr cannot read a non-ASCII GDAL_DATA dir.

Colleague failure 2026-07-04: app installed under ``E:\\图\\...`` -> ogr2ogr
"ERROR 4: Failed to find template header file header.dxf" even though Python
saw the file. ``ascii_safe_env_dir`` must hand ogr2ogr an ASCII-only path.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from geoscan import production_accuracy_workflow as paw


def _make_chinese_gdal_data(tmp_path: Path) -> Path:
    gdal_data = tmp_path / "图测试" / "gdal-data"
    gdal_data.mkdir(parents=True)
    (gdal_data / "header.dxf").write_text("0\nEOF\n", encoding="utf-8")
    (gdal_data / "trailer.dxf").write_text("0\nEOF\n", encoding="utf-8")
    return gdal_data


def test_ascii_path_passes_through_unchanged(tmp_path: Path) -> None:
    plain = tmp_path / "gdal-data"
    plain.mkdir()

    assert paw.ascii_safe_env_dir(plain, purpose="GDAL_DATA") == plain


def test_non_ascii_uses_short_path_when_it_is_ascii(monkeypatch, tmp_path: Path) -> None:
    gdal_data = _make_chinese_gdal_data(tmp_path)
    short = tmp_path / "TU~1" / "gdal-data"
    monkeypatch.setattr(paw, "_windows_short_path", lambda path: short)

    assert paw.ascii_safe_env_dir(gdal_data, purpose="GDAL_DATA") == short


def test_non_ascii_copies_to_ascii_temp_when_short_path_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    gdal_data = _make_chinese_gdal_data(tmp_path)
    temp_root = tmp_path / "ascii_temp"
    temp_root.mkdir()
    monkeypatch.setattr(paw, "_windows_short_path", lambda path: None)
    monkeypatch.setattr(paw, "_ascii_temp_root", lambda: temp_root)

    safe = paw.ascii_safe_env_dir(gdal_data, purpose="GDAL_DATA")

    assert safe == temp_root / "gdal_data"
    assert (safe / "header.dxf").is_file()
    assert (safe / "trailer.dxf").is_file()
    # Source is untouched.
    assert (gdal_data / "header.dxf").is_file()


def test_non_ascii_without_any_fallback_fails_loudly(monkeypatch, tmp_path: Path) -> None:
    gdal_data = _make_chinese_gdal_data(tmp_path)
    monkeypatch.setattr(paw, "_windows_short_path", lambda path: None)
    monkeypatch.setattr(paw, "_ascii_temp_root", lambda: None)

    with pytest.raises(RuntimeError, match="全英文路径"):
        paw.ascii_safe_env_dir(gdal_data, purpose="GDAL_DATA")


def test_startup_problem_message_only_when_unfixable(monkeypatch, tmp_path: Path) -> None:
    gdal_data = _make_chinese_gdal_data(tmp_path)
    monkeypatch.setattr(paw, "resolve_gdal_data", lambda path=None: gdal_data)
    monkeypatch.setattr(paw, "_windows_short_path", lambda path: None)

    monkeypatch.setattr(paw, "_ascii_temp_root", lambda: tmp_path)
    assert paw.non_ascii_install_path_problem() is None

    monkeypatch.setattr(paw, "_ascii_temp_root", lambda: None)
    message = paw.non_ascii_install_path_problem()
    assert message is not None
    assert "全英文路径" in message


def test_export_dxf_hands_ogr2ogr_an_ascii_gdal_data_env(monkeypatch, tmp_path: Path) -> None:
    gdal_data = _make_chinese_gdal_data(tmp_path)
    temp_root = tmp_path / "ascii_temp"
    temp_root.mkdir()
    monkeypatch.setattr(paw, "_windows_short_path", lambda path: None)
    monkeypatch.setattr(paw, "_ascii_temp_root", lambda: temp_root)

    ogr2ogr = tmp_path / "ogr2ogr.exe"
    ogr2ogr.write_bytes(b"stub")
    geojson = tmp_path / "sample.geojson"
    geojson.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
    dxf_path = tmp_path / "out" / "sample.dxf"

    captured: dict[str, str] = {}

    def fake_run(cmd, *, env, **kwargs):
        captured.update(env)
        Path(cmd[3]).write_text("0\nEOF\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(paw.subprocess, "run", fake_run)
    monkeypatch.setattr(paw, "make_dxf_mapgis_chinese_compatible", lambda path: None)

    report = paw._export_dxf(
        source_geojson=geojson,
        dxf_path=dxf_path,
        ogr2ogr_path=ogr2ogr,
        gdal_data=gdal_data,
    )

    assert captured["GDAL_DATA"] == str(temp_root / "gdal_data")
    assert all(ord(char) < 128 for char in captured["GDAL_DATA"])
    assert report["gdal_data_used"] == str(temp_root / "gdal_data")
