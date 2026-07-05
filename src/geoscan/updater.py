"""Client-side auto-update against GitHub Releases.

GeoScan is distributed as a Windows installer (``MapGISVectorizeSetup.exe``)
published as a GitHub Release asset. This module lets an installed copy check
whether a newer release exists, download the new installer, verify it, and hand
off to it — the installer then upgrades in place while user config in
``%LOCALAPPDATA%\\MapGISVectorize\\config`` is left untouched.

Security posture (public repo — no secrets needed):
  - The repo is public, so release assets are downloadable with NO credentials.
    Nothing sensitive is embedded here.
  - Transport is HTTPS to api.github.com / the GitHub asset CDN.
  - When the release asset carries a ``digest`` (``sha256:...``, which GitHub
    now records for uploaded assets) the downloaded file is verified against it.
    If absent, HTTPS is the integrity guard.

Dependency-free on purpose (stdlib ``urllib`` only) so it works inside the
frozen one-folder build without pulling extra wheels.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from geoscan import __version__

# The public repo that hosts releases. Update-check URL is derived from it.
GITHUB_REPO = "keros68/geoscan"
RELEASES_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# The installer asset to look for on a release. Must match the Inno Setup
# OutputBaseFilename in release/installer/installer.iss.
INSTALLER_ASSET_NAME = "MapGISVectorizeSetup.exe"

# GitHub's API requires a User-Agent; identify ourselves plainly.
_USER_AGENT = f"GeoScan-Updater/{__version__} (+https://github.com/{GITHUB_REPO})"


@dataclass(frozen=True)
class UpdateInfo:
    """Result of an update check."""

    current: str
    latest: str
    update_available: bool
    tag: str = ""
    installer_url: str = ""
    installer_size: int = 0
    installer_sha256: str = ""  # lowercase hex, or "" if the release didn't record one
    notes: str = ""
    html_url: str = ""


class UpdateError(RuntimeError):
    """Any failure while checking for or fetching an update."""


# ---------------------------------------------------------------------------
# Version comparison (loose semver; tolerant of a leading "v")
# ---------------------------------------------------------------------------
def _parse_version(v: str) -> tuple[int, ...]:
    v = str(v).strip().lstrip("vV")
    out: list[int] = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(remote: str, local: str) -> bool:
    """True when ``remote`` is a strictly higher version than ``local``."""
    return _parse_version(remote) > _parse_version(local)


def current_version() -> str:
    return __version__


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def _http_get(url: str, timeout: float, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _USER_AGENT)
    if accept:
        req.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed HTTPS hosts
            return resp.read()
    except urllib.error.HTTPError as exc:  # 404 (no releases yet), 403 (rate limit), ...
        raise UpdateError(_explain_http_error(exc)) from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"无法连接更新服务器：{exc.reason}") from exc
    except TimeoutError as exc:
        raise UpdateError("连接更新服务器超时，请检查网络后重试。") from exc


def _explain_http_error(exc: urllib.error.HTTPError) -> str:
    if exc.code == 404:
        return "尚未发布任何正式版本（GitHub Releases 为空）。"
    if exc.code == 403:
        return "GitHub 接口访问受限（可能触发了匿名频率限制），请稍后再试。"
    return f"更新服务器返回错误 HTTP {exc.code}。"


def _pick_installer_asset(assets: list[dict]) -> dict | None:
    # Exact filename first, then any .exe as a fallback.
    for asset in assets:
        if asset.get("name") == INSTALLER_ASSET_NAME:
            return asset
    for asset in assets:
        if str(asset.get("name", "")).lower().endswith(".exe"):
            return asset
    return None


def _asset_sha256(asset: dict) -> str:
    # GitHub records an asset digest like "sha256:abcd..." when available.
    digest = str(asset.get("digest", ""))
    if digest.startswith("sha256:"):
        return digest[len("sha256:"):].lower()
    return ""


def check_for_update(timeout: float = 12.0) -> UpdateInfo:
    """Query the latest GitHub release and compare it with the running version.

    Raises UpdateError on any network/parse failure so the GUI can show a
    friendly message. Never raises for the ordinary "already up to date" case.
    """
    raw = _http_get(RELEASES_LATEST_API, timeout, accept="application/vnd.github+json")
    try:
        release = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdateError("更新信息解析失败（服务器返回了非预期内容）。") from exc

    if release.get("draft") or release.get("prerelease"):
        # /releases/latest already excludes these, but be defensive.
        return UpdateInfo(current=__version__, latest=__version__, update_available=False)

    tag = str(release.get("tag_name", "")).strip()
    latest = tag.lstrip("vV") or "0.0.0"
    asset = _pick_installer_asset(release.get("assets") or [])

    available = is_newer(latest, __version__) and asset is not None
    return UpdateInfo(
        current=__version__,
        latest=latest,
        update_available=available,
        tag=tag,
        installer_url=str(asset.get("browser_download_url", "")) if asset else "",
        installer_size=int(asset.get("size", 0)) if asset else 0,
        installer_sha256=_asset_sha256(asset) if asset else "",
        notes=str(release.get("body", "") or ""),
        html_url=str(release.get("html_url", "")),
    )


def download_installer(
    info: UpdateInfo,
    dest_dir: Path | None = None,
    timeout: float = 300.0,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download the release installer, verifying sha256 when the release has one.

    Returns the path to the downloaded ``.exe``. ``progress(done, total)`` is
    called as bytes arrive (total may be 0 if the server omits Content-Length).
    """
    if not info.installer_url:
        raise UpdateError("该版本没有可下载的安装包资产。")

    dest_dir = dest_dir or Path(tempfile.mkdtemp(prefix="geoscan_update_"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / INSTALLER_ASSET_NAME

    req = urllib.request.Request(info.installer_url)
    req.add_header("User-Agent", _USER_AGENT)
    hasher = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            total = int(resp.headers.get("Content-Length") or info.installer_size or 0)
            done = 0
            with open(target, "wb") as fh:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    hasher.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"下载安装包失败：{exc}") from exc

    if info.installer_sha256:
        got = hasher.hexdigest().lower()
        if got != info.installer_sha256:
            try:
                target.unlink()
            except OSError:
                pass
            raise UpdateError(
                "安装包校验失败（sha256 不匹配），已删除下载文件。请重试或从项目主页手动下载。"
            )
    return target


def launch_installer_and_exit(installer: Path) -> None:
    """Start the downloaded installer and quit this process.

    The running exe/dll are locked on Windows, so the installer — a separate
    process writing to the same install dir — performs the swap. We exit
    immediately so nothing stays locked.
    """
    installer = Path(installer)
    if not installer.is_file():
        raise UpdateError(f"安装包不存在：{installer}")
    if sys.platform == "win32":
        # Detached so it survives our exit; no shell.
        subprocess.Popen(  # noqa: S603
            [str(installer)],
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    else:  # pragma: no cover - installer is Windows-only
        subprocess.Popen([str(installer)], close_fds=True)  # noqa: S603
    os._exit(0)  # hard-exit so no lingering Tk/atexit re-locks files


__all__ = [
    "GITHUB_REPO",
    "UpdateInfo",
    "UpdateError",
    "current_version",
    "is_newer",
    "check_for_update",
    "download_installer",
    "launch_installer_and_exit",
]
