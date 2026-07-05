"""Machine-local settings for the standalone/packaged workflow program.

Colleagues' machines will not have this repo's hardcoded tool paths
(``D:\\Qgis``, ``D:\\mapgis67``, the OCR conda env). The packaged program
reads a ``mapgis_settings.json`` placed next to the executable (or in the
working directory) and exports the values as the environment variables the
pipeline already honors:

- ``ogr2ogr``      -> ``MAPGIS_OGR2OGR``      (DXF export)
- ``gdal_data``    -> ``MAPGIS_GDAL_DATA``    (DXF export)
- ``section_exe``  -> ``MAPGIS67_SECTION_EXE`` (bridge conversion)
- ``w60_conv_exe`` -> ``MAPGIS67_W60_CONV_EXE`` (bridge conversion)
- ``ocr_python``   -> ``MAPGIS_OCR_PYTHON``   (external OCR interpreter)

Resolution order stays: explicit argument > environment variable > settings
file > built-in default. Already-set environment variables are never
overwritten, and the settings file never contains API keys (AI keys are
pasted per session in the GUI and never persisted).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SETTINGS_FILENAME = "mapgis_settings.json"

SETTINGS_ENV_KEYS: dict[str, str] = {
    "ogr2ogr": "MAPGIS_OGR2OGR",
    "gdal_data": "MAPGIS_GDAL_DATA",
    "section_exe": "MAPGIS67_SECTION_EXE",
    "w60_conv_exe": "MAPGIS67_W60_CONV_EXE",
    "ocr_python": "MAPGIS_OCR_PYTHON",
}

# Persisted for the GUI only; never exported to env. AI settings cover the
# provider/url/model ONLY — the API key is pasted per session and never saved.
GUI_ONLY_KEYS = ("project_root", "ai_provider", "ai_base_url", "ai_model", "ai_enhance")

FORBIDDEN_KEY_FRAGMENTS = ("api_key", "apikey", "token", "secret")

# Per-user, writable, update-surviving config location. Installed apps live in a
# read-only folder (Program Files) that a self-updater rewrites wholesale, so
# settings and the encrypted key must NOT sit next to the exe. They move to
# %LOCALAPPDATA%\MapGISVectorize\config\. A machine-local override env var helps
# tests and power users. NOTE: this dir may contain a non-ASCII (Chinese)
# username; only plain config text goes here — never GDAL_DATA/PROJ_LIB or the
# gdal bundle, which ogr2ogr cannot read from a non-ASCII path.
CONFIG_DIR_ENV = "MAPGIS_CONFIG_DIR"
APP_CONFIG_SUBDIR = ("MapGISVectorize", "config")


def config_dir() -> Path:
    """Per-user writable config dir (survives app updates).

    Resolution: ``MAPGIS_CONFIG_DIR`` override > ``%LOCALAPPDATA%`` >
    ``%APPDATA%`` > a ``.mapgis_config`` folder under the cwd (last resort). Pure
    — never creates the directory; writers ``mkdir(parents=True)`` before saving,
    so merely reading settings has no filesystem side effect.
    """
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base and base.strip():
        return Path(base.strip()).joinpath(*APP_CONFIG_SUBDIR)
    return Path.cwd() / ".mapgis_config"


def settings_search_paths() -> list[Path]:
    """Read order: per-user config dir (primary) > executable folder (legacy
    frozen builds) > working directory (legacy/dev)."""
    paths: list[Path] = [config_dir() / SETTINGS_FILENAME]
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / SETTINGS_FILENAME)
    paths.append(Path.cwd() / SETTINGS_FILENAME)
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def load_settings(path: Path) -> dict[str, str]:
    payload: Any = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    settings: dict[str, str] = {}
    for key, value in payload.items():
        key_lower = str(key).strip().lower()
        if any(fragment in key_lower for fragment in FORBIDDEN_KEY_FRAGMENTS):
            raise ValueError(
                f"{path} must not contain credentials (offending key: {key}); "
                "AI keys are pasted per session in the GUI and never saved."
            )
        if (key_lower in SETTINGS_ENV_KEYS or key_lower in GUI_ONLY_KEYS) and str(value).strip():
            settings[key_lower] = str(value).strip()
    return settings


def apply_settings_to_env(settings: dict[str, str], *, override: bool = False) -> dict[str, str]:
    """Export settings as env vars.

    By default existing env vars win and are not touched; ``override=True`` is
    for an explicit in-session change (e.g. the GUI settings page save button).
    """
    applied: dict[str, str] = {}
    for key, env_name in SETTINGS_ENV_KEYS.items():
        value = settings.get(key)
        if not value:
            continue
        if not override and os.environ.get(env_name, "").strip():
            continue
        os.environ[env_name] = value
        applied[env_name] = value
    return applied


def settings_save_path() -> Path:
    """Where the GUI persists settings and the encrypted key.

    New writes ALWAYS go to the per-user config dir, so an updated/read-only
    install folder is never written and self-updates never clobber user
    settings. Legacy files next to the exe are still *read* (see
    ``settings_search_paths``) and migrated in on first launch, but not written
    back.
    """
    return config_dir() / SETTINGS_FILENAME


def save_settings(settings: dict[str, str], path: Path | None = None) -> Path:
    """Persist known settings keys to ``mapgis_settings.json``; never credentials.

    Known keys with empty values are removed from the file; unknown keys already
    present in an existing file are preserved untouched.
    """
    target = Path(path) if path is not None else settings_save_path()
    cleaned: dict[str, str] = {}
    for key, value in settings.items():
        key_lower = str(key).strip().lower()
        if any(fragment in key_lower for fragment in FORBIDDEN_KEY_FRAGMENTS):
            raise ValueError(
                f"Refusing to save credentials (offending key: {key}); "
                "AI keys are pasted per session in the GUI and never saved."
            )
        if key_lower not in SETTINGS_ENV_KEYS and key_lower not in GUI_ONLY_KEYS:
            continue
        text = str(value).strip()
        if text:
            cleaned[key_lower] = text

    existing: dict[str, Any] = {}
    if target.is_file():
        try:
            payload = json.loads(target.read_text(encoding="utf-8-sig"))
            if isinstance(payload, dict):
                existing = payload
        except json.JSONDecodeError:
            existing = {}

    merged: dict[str, Any] = dict(existing)
    for key in (*SETTINGS_ENV_KEYS, *GUI_ONLY_KEYS):
        merged.pop(key, None)
    merged.update(cleaned)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


AI_KEY_FILENAME = "mapgis_ai_key.dat"
_AI_KEY_ENTROPY = b"mapgis_vectorize_gui.ai_api_key.v1"


def ai_key_file_path() -> Path:
    """The DPAPI-encrypted API key lives NEXT TO the settings file, never inside it."""
    return settings_save_path().parent / AI_KEY_FILENAME


def save_encrypted_api_key(api_key: str, path: Path | None = None) -> Path | None:
    """Persist the AI API key encrypted with Windows DPAPI (current user scope).

    The blob only decrypts for the same Windows user on the same machine, so
    copying the app folder to another PC or user does NOT leak the key.
    Passing an empty key deletes the stored file.
    """
    target = Path(path) if path is not None else ai_key_file_path()
    value = str(api_key or "").strip()
    if not value:
        if target.is_file():
            target.unlink()
        return None
    import win32crypt

    blob = win32crypt.CryptProtectData(
        value.encode("utf-8"), "mapgis ai api key", _AI_KEY_ENTROPY, None, None, 0
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    return target


def load_encrypted_api_key(path: Path | None = None) -> str:
    """Best-effort decrypt of the stored AI key; empty string when absent/undecryptable."""
    target = Path(path) if path is not None else ai_key_file_path()
    if not target.is_file():
        return ""
    try:
        import win32crypt

        _description, data = win32crypt.CryptUnprotectData(
            target.read_bytes(), _AI_KEY_ENTROPY, None, None, 0
        )
        return data.decode("utf-8")
    except Exception:
        return ""


def migrate_legacy_config() -> dict[str, Any]:
    """One-time best-effort copy of a legacy ``mapgis_settings.json`` /
    ``mapgis_ai_key.dat`` from the executable folder into the per-user config
    dir, so upgrading from an old build that wrote next to the exe keeps the
    user's tool paths and (DPAPI, same-user-same-machine) key. Never overwrites
    an existing config-dir file; leaves the legacy files untouched. Safe no-op
    off frozen builds (dev/source runs have no separate exe folder)."""
    result: dict[str, Any] = {"migrated": []}
    if not getattr(sys, "frozen", False):
        return result
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except (OSError, ValueError):
        return result
    dest_dir = config_dir()
    if exe_dir == dest_dir:
        return result
    for name in (SETTINGS_FILENAME, AI_KEY_FILENAME):
        legacy = exe_dir / name
        target = dest_dir / name
        if legacy.is_file() and not target.exists():
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(legacy.read_bytes())
                result["migrated"].append(name)
            except OSError:
                pass
    return result


def read_machine_settings() -> dict[str, str]:
    """Best-effort read of the first settings file; empty dict when none/invalid."""
    for candidate in settings_search_paths():
        if not candidate.is_file():
            continue
        try:
            return load_settings(candidate)
        except (ValueError, json.JSONDecodeError):
            return {}
    return {}


def bootstrap_settings() -> dict[str, Any]:
    """Find and apply the first settings file; safe no-op when none exists."""
    migrate_legacy_config()
    for candidate in settings_search_paths():
        if not candidate.is_file():
            continue
        try:
            settings = load_settings(candidate)
        except (ValueError, json.JSONDecodeError) as exc:
            return {"settings_file": str(candidate), "ok": False, "error": str(exc)}
        applied = apply_settings_to_env(settings)
        return {
            "settings_file": str(candidate),
            "ok": True,
            "applied_env": applied,
            "note": "explicit args and pre-set environment variables still take precedence",
        }
    return {"settings_file": None, "ok": True, "applied_env": {}}
