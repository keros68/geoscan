"""Client-side auto-update against mirrored manifests + GitHub Releases.

GeoScan installs as two layers:
  * **runtime** — the frozen Python + numpy/cv2/onnxruntime/rapidocr/... (~100 MB,
    changes rarely). Shipped only via the full installer ``GeoScanSetup.exe``.
  * **engine** — the loose ``geoscan`` package under ``<_internal>/engine/`` (~1 MB,
    changes every release). Shipped as a tiny ``engine-<ver>-rt<N>.zip`` asset.

So a normal code update downloads ~250 KB (the engine zip) and swaps the loose
folder in place, instead of re-downloading the whole installer. When the runtime
itself changes (rare), the release ships an engine zip tagged with a new ``rt``
number that no installed client matches, and the client falls back to the full
installer.

Release assets per version:
  * ``GeoScanSetup.exe``            — full installer (new installs, runtime bumps)
  * ``engine-<ver>-rt<N>.zip``      — engine-only update for runtime line ``N``

Security posture (public repo — no secrets needed): assets are downloadable with
no credentials; transport is HTTPS; each download is sha256-verified against the
mirror manifest's ``sha256`` or GitHub-recorded asset ``digest`` when present.
Stdlib-only (urllib/zipfile) so it works inside the frozen build with no extra
wheels.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from geoscan import __version__

# The public repo that hosts releases. Update-check URL is derived from it.
GITHUB_REPO = "keros68/geoscan"
RELEASES_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Domestic/near-China mirror checked before GitHub. Set GEOSCAN_UPDATE_MANIFEST_URL
# to another HTTPS latest.json endpoint when the download host changes.
DEFAULT_UPDATE_MANIFEST_URL = "https://aidraw.cv/geoscan-updates/latest.json"
UPDATE_MANIFEST_ENV = "GEOSCAN_UPDATE_MANIFEST_URL"

# The installer asset to look for on a release. Must match the Inno Setup
# OutputBaseFilename in release/installer/installer.iss.
INSTALLER_ASSET_NAME = "GeoScanSetup.exe"

# Engine asset name: engine-<engineVersion>-rt<runtimeVersion>.zip
_ENGINE_ASSET_RE = re.compile(r"^engine-(?P<ver>[0-9][0-9A-Za-z.\-]*)-rt(?P<rt>[0-9A-Za-z.]+)\.zip$")

# Fallback when no runtime_version.txt ships (older/dev builds).
RUNTIME_VERSION_FALLBACK = "1"

# GitHub's API requires a User-Agent; identify ourselves plainly.
_USER_AGENT = f"GeoScan-Updater/{__version__} (+https://github.com/{GITHUB_REPO})"


@dataclass(frozen=True)
class UpdateInfo:
    """Result of an update check."""

    current: str
    latest: str
    update_available: bool
    kind: str = "none"  # "engine" (lightweight) | "installer" (full) | "none"
    tag: str = ""
    # Full installer (also the fallback when an engine update is not applicable).
    installer_url: str = ""
    installer_size: int = 0
    installer_sha256: str = ""
    # Lightweight engine zip.
    engine_url: str = ""
    engine_size: int = 0
    engine_sha256: str = ""
    notes: str = ""
    html_url: str = ""

    @property
    def download_size(self) -> int:
        return self.engine_size if self.kind == "engine" else self.installer_size


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
# Install layout (frozen two-layer build)
# ---------------------------------------------------------------------------
def _frozen_internal_dir() -> Path | None:
    """The frozen build's ``_internal`` dir (``sys._MEIPASS``), or None in dev."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        if base:
            return Path(base)
    return None


def installed_runtime_version() -> str:
    """Runtime-layer version, from ``runtime_version.txt`` shipped in the build."""
    base = _frozen_internal_dir()
    if base is not None:
        path = base / "runtime_version.txt"
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip() or RUNTIME_VERSION_FALLBACK
        except OSError:
            pass
    return RUNTIME_VERSION_FALLBACK


def engine_dir() -> Path | None:
    """The loose engine dir (``<_internal>/engine``) holding ``geoscan/``, or None."""
    base = _frozen_internal_dir()
    if base is not None:
        candidate = base / "engine"
        if (candidate / "geoscan" / "__init__.py").is_file():
            return candidate
    return None


def _engine_writable() -> bool:
    """Whether we can write into the engine dir (per-user installs: yes)."""
    directory = engine_dir()
    if directory is None:
        return False
    try:
        probe = directory / ".geoscan_write_probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


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


def _release_sources() -> list[tuple[str, str | None]]:
    """Ordered update metadata sources: mirror first, then GitHub fallback."""
    sources: list[tuple[str, str | None]] = []
    mirror = os.environ.get(UPDATE_MANIFEST_ENV, DEFAULT_UPDATE_MANIFEST_URL).strip()
    if mirror:
        sources.append((mirror, None))
    sources.append((RELEASES_LATEST_API, "application/vnd.github+json"))

    unique: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for url, accept in sources:
        if url not in seen:
            unique.append((url, accept))
            seen.add(url)
    return unique


def _download_to(
    url: str,
    target: Path,
    sha256: str,
    size_hint: int,
    timeout: float,
    progress: Callable[[int, int], None] | None,
) -> None:
    """Stream a URL to ``target``, verifying sha256 when one is given."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _USER_AGENT)
    hasher = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            total = int(resp.headers.get("Content-Length") or size_hint or 0)
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
        raise UpdateError(f"下载失败：{exc}") from exc

    if sha256 and hasher.hexdigest().lower() != sha256.lower():
        try:
            target.unlink()
        except OSError:
            pass
        raise UpdateError("下载文件校验失败（sha256 不匹配），已删除。请重试或从项目主页手动下载。")


def _pick_installer_asset(assets: list[dict]) -> dict | None:
    for asset in assets:
        if asset.get("name") == INSTALLER_ASSET_NAME:
            return asset
    for asset in assets:  # fallback: any .exe
        if str(asset.get("name", "")).lower().endswith(".exe"):
            return asset
    return None


def _pick_engine_asset(assets: list[dict], latest: str, runtime: str) -> dict | None:
    """The engine zip for this release version AND the installed runtime line."""
    for asset in assets:
        match = _ENGINE_ASSET_RE.match(str(asset.get("name", "")))
        if (
            match
            and _parse_version(match.group("ver")) == _parse_version(latest)
            and match.group("rt") == str(runtime)
        ):
            return asset
    return None


def _asset_sha256(asset: dict) -> str:
    digest = str(asset.get("digest", ""))
    if digest.startswith("sha256:"):
        return digest[len("sha256:"):].lower()
    sha256 = str(asset.get("sha256", "")).strip().lower()
    if sha256.startswith("sha256:"):
        sha256 = sha256[len("sha256:"):]
    if re.fullmatch(r"[0-9a-f]{64}", sha256):
        return sha256
    return ""


def _normalise_asset(asset: dict) -> dict:
    """Accept both GitHub release assets and our static mirror asset shape."""
    out = dict(asset)
    if not out.get("browser_download_url") and out.get("url"):
        out["browser_download_url"] = str(out["url"])
    if out.get("sha256") and not out.get("digest"):
        sha256 = str(out["sha256"]).strip()
        out["digest"] = sha256 if sha256.startswith("sha256:") else f"sha256:{sha256}"
    return out


def _normalise_release(release: dict) -> dict:
    """Accept GitHub /releases/latest JSON or GeoScan's static latest.json."""
    if not isinstance(release, dict):
        raise UpdateError("更新信息格式异常（不是 JSON 对象）。")

    out = dict(release)
    tag = str(out.get("tag_name") or out.get("tag") or "").strip()
    version = str(out.get("version") or "").strip().lstrip("vV")
    if not tag and version:
        tag = f"v{version}"
    out["tag_name"] = tag
    if "body" not in out:
        out["body"] = str(out.get("notes", "") or "")
    if "html_url" not in out:
        out["html_url"] = str(out.get("github", "") or "")
    assets = out.get("assets") or []
    if not isinstance(assets, list):
        raise UpdateError("更新信息格式异常（assets 不是数组）。")
    out["assets"] = [_normalise_asset(asset) for asset in assets if isinstance(asset, dict)]
    return out


def _fetch_latest_release(timeout: float) -> dict:
    errors: list[str] = []
    for url, accept in _release_sources():
        try:
            raw = _http_get(url, timeout, accept=accept)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise UpdateError("更新信息解析失败（服务器返回了非预期内容）。") from exc
            return _normalise_release(payload)
        except UpdateError as exc:
            errors.append(f"{url}: {exc}")
    raise UpdateError("所有更新源均不可用：" + "；".join(errors))


def check_for_update(timeout: float = 12.0) -> UpdateInfo:
    """Query update metadata and decide whether/how to update.

    Prefers a lightweight engine update when the release ships an engine zip for
    the installed runtime line and the engine dir is writable; otherwise falls
    back to the full installer. Raises UpdateError on network/parse failure;
    never raises for the ordinary "already up to date" case.
    """
    release = _fetch_latest_release(timeout)

    if release.get("draft") or release.get("prerelease"):
        return UpdateInfo(current=__version__, latest=__version__, update_available=False)

    tag = str(release.get("tag_name", "")).strip()
    latest = tag.lstrip("vV") or "0.0.0"
    notes = str(release.get("body", "") or "")
    html_url = str(release.get("html_url", ""))

    if not is_newer(latest, __version__):
        return UpdateInfo(
            current=__version__, latest=latest, update_available=False,
            tag=tag, notes=notes, html_url=html_url,
        )

    assets = release.get("assets") or []
    installer = _pick_installer_asset(assets)
    engine = _pick_engine_asset(assets, latest, installed_runtime_version())

    installer_fields = dict(
        installer_url=str(installer.get("browser_download_url", "")) if installer else "",
        installer_size=int(installer.get("size", 0)) if installer else 0,
        installer_sha256=_asset_sha256(installer) if installer else "",
    )

    # Lightweight path: an engine zip matching our runtime, and a writable engine dir.
    if engine is not None and engine_dir() is not None and _engine_writable():
        return UpdateInfo(
            current=__version__, latest=latest, update_available=True, kind="engine",
            tag=tag, notes=notes, html_url=html_url,
            engine_url=str(engine.get("browser_download_url", "")),
            engine_size=int(engine.get("size", 0)),
            engine_sha256=_asset_sha256(engine),
            **installer_fields,  # keep installer as a fallback
        )

    if installer is not None:
        return UpdateInfo(
            current=__version__, latest=latest, update_available=True, kind="installer",
            tag=tag, notes=notes, html_url=html_url, **installer_fields,
        )

    # Newer version exists but nothing we can install.
    return UpdateInfo(
        current=__version__, latest=latest, update_available=False,
        tag=tag, notes=notes, html_url=html_url,
    )


# ---------------------------------------------------------------------------
# Full installer path
# ---------------------------------------------------------------------------
def download_installer(
    info: UpdateInfo,
    dest_dir: Path | None = None,
    timeout: float = 300.0,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download the release installer, verifying sha256 when the release has one."""
    if not info.installer_url:
        raise UpdateError("该版本没有可下载的安装包资产。")
    dest_dir = dest_dir or Path(tempfile.mkdtemp(prefix="geoscan_update_"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / INSTALLER_ASSET_NAME
    _download_to(info.installer_url, target, info.installer_sha256, info.installer_size, timeout, progress)
    return target


def launch_installer_and_exit(installer: Path) -> None:
    """Start the downloaded installer and quit this process."""
    installer = Path(installer)
    if not installer.is_file():
        raise UpdateError(f"安装包不存在：{installer}")
    _spawn_detached([str(installer)])
    os._exit(0)


# ---------------------------------------------------------------------------
# Lightweight engine path
# ---------------------------------------------------------------------------
def download_engine(
    info: UpdateInfo,
    dest_dir: Path | None = None,
    timeout: float = 120.0,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download + verify + extract the engine zip. Returns the extracted dir
    (which contains a ``geoscan/`` package)."""
    if not info.engine_url:
        raise UpdateError("该版本没有可下载的引擎包。")
    dest_dir = dest_dir or Path(tempfile.mkdtemp(prefix="geoscan_engine_"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "engine.zip"
    _download_to(info.engine_url, zip_path, info.engine_sha256, info.engine_size, timeout, progress)

    extract_dir = dest_dir / "extracted"
    extract_dir.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(f"引擎包解压失败：{exc}") from exc
    if not (extract_dir / "geoscan" / "__init__.py").is_file():
        raise UpdateError("引擎包结构异常（缺少 geoscan/ 包）。")
    return extract_dir


def apply_engine_update(staging: Path) -> None:
    """Replace the live engine's ``geoscan/`` with the staged copy.

    Loose .py files are not locked on Windows once imported, so we overwrite in
    place. The engine zip carries the FULL public package, so afterwards
    anything in the live package that the staged copy does not ship (a module
    removed upstream, old bytecode) is stale and gets swept — leftovers would
    otherwise survive updates forever and can shadow current code. Copy first,
    sweep second: the package stays complete at every moment, so a crash
    mid-update never leaves a missing-module engine. A stale file that cannot
    be deleted is skipped rather than failing the whole update (no worse than
    the old overwrite-only behavior).
    """
    live = engine_dir()
    if live is None:
        raise UpdateError("未找到引擎目录，无法应用引擎更新。")
    src_pkg = Path(staging) / "geoscan"
    dst_pkg = live / "geoscan"
    if not src_pkg.is_dir():
        raise UpdateError("引擎包缺少 geoscan/。")
    try:
        shipped: set[Path] = set()
        for root, _dirs, files in os.walk(src_pkg):
            rel = Path(root).relative_to(src_pkg)
            target_dir = dst_pkg / rel
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in files:
                shutil.copy2(Path(root) / name, target_dir / name)
                shipped.add(rel / name)
    except OSError as exc:
        raise UpdateError(f"应用引擎更新失败：{exc}") from exc
    for root, _dirs, files in os.walk(dst_pkg, topdown=False):
        rel = Path(root).relative_to(dst_pkg)
        for name in files:
            if rel / name not in shipped:
                with contextlib.suppress(OSError):
                    (Path(root) / name).unlink()
        if rel != Path(".") and not os.listdir(root):
            with contextlib.suppress(OSError):
                os.rmdir(root)


def apply_engine_update_and_restart(staging: Path) -> None:
    """Apply the engine update, then relaunch the app and exit."""
    apply_engine_update(staging)
    _spawn_detached([sys.executable])
    os._exit(0)


# ---------------------------------------------------------------------------
def _spawn_detached(argv: list[str]) -> None:
    if sys.platform == "win32":
        subprocess.Popen(  # noqa: S603
            argv,
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    else:  # pragma: no cover - frozen app is Windows-only
        subprocess.Popen(argv, close_fds=True)  # noqa: S603


__all__ = [
    "DEFAULT_UPDATE_MANIFEST_URL",
    "GITHUB_REPO",
    "UpdateInfo",
    "UpdateError",
    "current_version",
    "is_newer",
    "installed_runtime_version",
    "engine_dir",
    "check_for_update",
    "download_installer",
    "launch_installer_and_exit",
    "download_engine",
    "apply_engine_update",
    "apply_engine_update_and_restart",
]
