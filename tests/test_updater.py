"""Tests for the GitHub-Releases auto-updater.

All network access is monkeypatched — these exercise version comparison,
release-JSON parsing, asset selection, sha256 verification, and the
download/handoff wiring. No real HTTP is ever made.
"""

from __future__ import annotations

import hashlib
import io
import json

import pytest

from geoscan import updater


# --------------------------------------------------------------------------
# version comparison
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("remote", "local", "expected"),
    [
        ("0.2.0", "0.1.0", True),
        ("v0.2.0", "0.1.0", True),  # tolerates leading v
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("1.0.0", "0.9.9", True),
        ("0.1.10", "0.1.2", True),  # numeric, not lexical
        ("0.1.0", "0.1.0.0", False),
    ],
)
def test_is_newer(remote, local, expected):
    assert updater.is_newer(remote, local) is expected


# --------------------------------------------------------------------------
# check_for_update: parsing the /releases/latest payload
# --------------------------------------------------------------------------
def _release_json(tag: str, *, asset_name=updater.INSTALLER_ASSET_NAME, size=123, digest=None, body="notes"):
    asset = {"name": asset_name, "browser_download_url": f"https://example/{asset_name}", "size": size}
    if digest:
        asset["digest"] = digest
    return json.dumps(
        {
            "tag_name": tag,
            "body": body,
            "html_url": "https://github.com/keros68/geoscan/releases/latest",
            "assets": [asset],
        }
    ).encode("utf-8")


def _patch_get(monkeypatch, payload: bytes):
    monkeypatch.setattr(updater, "_http_get", lambda url, timeout, accept=None: payload)


def test_check_reports_update_when_remote_is_newer(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    _patch_get(monkeypatch, _release_json("v0.2.0"))
    info = updater.check_for_update()
    assert info.update_available is True
    assert info.latest == "0.2.0"
    assert info.tag == "v0.2.0"
    assert info.installer_url.endswith(updater.INSTALLER_ASSET_NAME)


def test_check_reports_no_update_when_same_version(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.2.0")
    _patch_get(monkeypatch, _release_json("v0.2.0"))
    info = updater.check_for_update()
    assert info.update_available is False
    assert info.current == "0.2.0"


def test_check_no_update_when_release_has_no_installer_asset(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    _patch_get(monkeypatch, _release_json("v0.2.0", asset_name="notes.txt"))
    info = updater.check_for_update()
    # newer tag but no .exe asset -> nothing to install
    assert info.update_available is False


def test_check_parses_asset_sha256_digest(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    _patch_get(monkeypatch, _release_json("v0.2.0", digest="sha256:" + "ab" * 32))
    info = updater.check_for_update()
    assert info.installer_sha256 == "ab" * 32


def test_check_raises_updateerror_on_bad_json(monkeypatch):
    _patch_get(monkeypatch, b"<html>not json</html>")
    with pytest.raises(updater.UpdateError):
        updater.check_for_update()


def test_check_ignores_prerelease(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    payload = json.dumps({"tag_name": "v9.9.9", "prerelease": True, "assets": []}).encode()
    _patch_get(monkeypatch, payload)
    info = updater.check_for_update()
    assert info.update_available is False


# --------------------------------------------------------------------------
# download_installer: streaming + sha256 verification
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, data: bytes):
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=0: _FakeResp(data))


def test_download_writes_file_and_reports_progress(monkeypatch, tmp_path):
    data = b"MZ fake installer bytes" * 1000
    _patch_urlopen(monkeypatch, data)
    info = updater.UpdateInfo(
        current="0.1.0", latest="0.2.0", update_available=True,
        installer_url="https://example/setup.exe", installer_size=len(data),
    )
    seen = []
    out = updater.download_installer(info, dest_dir=tmp_path, progress=lambda d, t: seen.append((d, t)))
    assert out.read_bytes() == data
    assert seen and seen[-1][0] == len(data)


def test_download_verifies_sha256_and_rejects_mismatch(monkeypatch, tmp_path):
    data = b"the real bytes"
    _patch_urlopen(monkeypatch, data)
    info = updater.UpdateInfo(
        current="0.1.0", latest="0.2.0", update_available=True,
        installer_url="https://example/setup.exe",
        installer_sha256="00" * 32,  # deliberately wrong
    )
    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, dest_dir=tmp_path)
    # bad download is cleaned up
    assert not (tmp_path / updater.INSTALLER_ASSET_NAME).exists()


def test_download_accepts_matching_sha256(monkeypatch, tmp_path):
    data = b"the real bytes"
    _patch_urlopen(monkeypatch, data)
    good = hashlib.sha256(data).hexdigest()
    info = updater.UpdateInfo(
        current="0.1.0", latest="0.2.0", update_available=True,
        installer_url="https://example/setup.exe", installer_sha256=good,
    )
    out = updater.download_installer(info, dest_dir=tmp_path)
    assert out.read_bytes() == data


def test_download_without_url_raises(tmp_path):
    info = updater.UpdateInfo(current="0.1.0", latest="0.2.0", update_available=True)
    with pytest.raises(updater.UpdateError):
        updater.download_installer(info, dest_dir=tmp_path)


# --------------------------------------------------------------------------
# handoff
# --------------------------------------------------------------------------
def test_launch_missing_installer_raises(tmp_path):
    with pytest.raises(updater.UpdateError):
        updater.launch_installer_and_exit(tmp_path / "nope.exe")


def test_launch_starts_process_and_exits(monkeypatch, tmp_path):
    installer = tmp_path / updater.INSTALLER_ASSET_NAME
    installer.write_bytes(b"MZ")
    started = {}
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: started.setdefault("args", a))

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(updater.os, "_exit", fake_exit)
    with pytest.raises(SystemExit):
        updater.launch_installer_and_exit(installer)
    assert started["args"]  # Popen was called before exit


def test_repo_constant_matches_api_url():
    assert updater.GITHUB_REPO in updater.RELEASES_LATEST_API
    assert updater.RELEASES_LATEST_API.startswith("https://api.github.com/")


# --------------------------------------------------------------------------
# Two-layer engine update path
# --------------------------------------------------------------------------
def test_pick_engine_asset_matches_version_and_runtime():
    assets = [
        {"name": "engine-0.2.0-rt1.zip", "browser_download_url": "u1"},
        {"name": "engine-0.2.0-rt2.zip", "browser_download_url": "u2"},
        {"name": "GeoScanSetup.exe"},
    ]
    assert updater._pick_engine_asset(assets, "0.2.0", "1")["name"] == "engine-0.2.0-rt1.zip"
    assert updater._pick_engine_asset(assets, "0.2.0", "9") is None  # no matching runtime
    assert updater._pick_engine_asset(assets, "0.3.0", "1") is None  # version mismatch


def _release_with_engine(tag, rt):
    return json.dumps(
        {
            "tag_name": tag,
            "body": "notes",
            "assets": [
                {"name": "GeoScanSetup.exe", "browser_download_url": "https://x/GeoScanSetup.exe",
                 "size": 100_000, "digest": "sha256:" + "aa" * 32},
                {"name": f"engine-{tag.lstrip('v')}-rt{rt}.zip", "browser_download_url": "https://x/engine.zip",
                 "size": 2048, "digest": "sha256:" + "bb" * 32},
            ],
        }
    ).encode()


def test_check_prefers_lightweight_engine_update(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    monkeypatch.setattr(updater, "installed_runtime_version", lambda: "1")
    monkeypatch.setattr(updater, "engine_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_engine_writable", lambda: True)
    _patch_get(monkeypatch, _release_with_engine("v0.2.0", "1"))
    info = updater.check_for_update()
    assert info.update_available and info.kind == "engine"
    assert info.engine_url == "https://x/engine.zip"
    assert info.engine_sha256 == "bb" * 32
    assert info.download_size == 2048
    assert info.installer_url.endswith("GeoScanSetup.exe")  # fallback still carried


def test_check_falls_back_to_installer_on_runtime_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    monkeypatch.setattr(updater, "installed_runtime_version", lambda: "1")
    monkeypatch.setattr(updater, "engine_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_engine_writable", lambda: True)
    _patch_get(monkeypatch, _release_with_engine("v0.2.0", "2"))  # engine built for runtime 2
    info = updater.check_for_update()
    assert info.update_available and info.kind == "installer"


def test_apply_engine_update_overwrites_live_package(monkeypatch, tmp_path):
    live = tmp_path / "engine"
    (live / "geoscan").mkdir(parents=True)
    (live / "geoscan" / "__init__.py").write_text("__version__ = 'old'", encoding="utf-8")
    (live / "geoscan" / "keep.py").write_text("untouched", encoding="utf-8")
    staging = tmp_path / "staging"
    (staging / "geoscan").mkdir(parents=True)
    (staging / "geoscan" / "__init__.py").write_text("__version__ = 'new'", encoding="utf-8")

    monkeypatch.setattr(updater, "engine_dir", lambda: live)
    updater.apply_engine_update(staging)

    assert "new" in (live / "geoscan" / "__init__.py").read_text(encoding="utf-8")
    assert (live / "geoscan" / "keep.py").exists()  # pre-existing files remain


def test_download_engine_extracts_geoscan(monkeypatch, tmp_path):
    buf = io.BytesIO()
    with __import__("zipfile").ZipFile(buf, "w") as zf:
        zf.writestr("geoscan/__init__.py", "__version__ = '0.2.0'")
    data = buf.getvalue()
    _patch_urlopen(monkeypatch, data)
    info = updater.UpdateInfo(
        current="0.1.0", latest="0.2.0", update_available=True, kind="engine",
        engine_url="https://x/engine.zip", engine_size=len(data),
    )
    out = updater.download_engine(info, dest_dir=tmp_path)
    assert (out / "geoscan" / "__init__.py").is_file()
