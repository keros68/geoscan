from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BOOTSTRAP_FILE = Path(__file__).resolve().parents[0] / "section_bootstrap" / "SECTION_BOOTSTRAP.WT"

DEFAULT_DEPENDENCIES = {
    "cv2": "opencv-python",
    "numpy": "numpy",
    "PIL": "pillow",
    "win32gui": "pywin32",
    "mcp.server.fastmcp": "mcp",
    "rapidocr": "rapidocr",
    "rapidocr_onnxruntime": "rapidocr-onnxruntime",
}

MAPGIS_PROGRAM_NAMES = {
    "section": "section.exe",
    "mdiedit": "mdiedit6x.exe",
    "face67": "Face67.exe",
    "w60_conv": "W60_Conv.exe",
}

# MapGIS 6.7 dongle (加密狗/密码狗) service process. When the dongle is not
# plugged in / its service is not started, SECTION and W60_Conv still launch but
# the conversion silently produces no .WL/.WT and only fails after the full
# verification timeout (~300s of wasted work). Checking that this process is
# running is a fast, reliable pre-flight for that failure mode.
DONGLE_PROCESS_NAME = "dog67.exe"
DONGLE_PROCESS_SETTING_KEY = "dongle_process_name"
DONGLE_PROCESS_ENV_NAME = "MAPGIS67_DONGLE_PROCESS_NAME"


def normalize_dongle_process_name(value: object | None) -> str:
    """Normalize a user-supplied dongle process setting to a tasklist image name."""
    text = str(value or "").strip().strip('"')
    if not text:
        return DONGLE_PROCESS_NAME
    # Accept either a bare process name or a picked exe path from the settings UI.
    name = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    if not name:
        return DONGLE_PROCESS_NAME
    if "." not in name:
        name += ".exe"
    return name


def resolve_dongle_process_name(settings: Mapping[str, str] | None = None) -> str:
    """Configured dongle process image name, defaulting to ``dog67.exe``."""
    configured = os.environ.get(DONGLE_PROCESS_ENV_NAME, "").strip()
    if not configured and settings is not None:
        configured = str(settings.get(DONGLE_PROCESS_SETTING_KEY) or "").strip()
    return normalize_dongle_process_name(configured)


def dongle_process_running(process_name: str | None = None) -> bool:
    """Return True if the MapGIS dongle service process is currently running.

    Windows-only (uses ``tasklist``). On any other platform, or if the query
    itself fails, returns False (treated as "cannot confirm the dongle").
    """
    process_name = resolve_dongle_process_name(
        {DONGLE_PROCESS_SETTING_KEY: process_name} if process_name else None
    )
    if not sys.platform.startswith("win"):
        return False
    import subprocess

    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # tasklist prints the process line when running, or an "INFO: No tasks..."
    # notice otherwise. Search the raw bytes for the name (ASCII), decoding
    # loosely because the notice text is localized (GBK on Chinese Windows).
    output = completed.stdout.decode("utf-8", "ignore").lower()
    return process_name.lower() in output


def dongle_status(process_name: str | None = None) -> dict:
    """Structured dongle pre-flight result for reports and the GUI status line."""
    process_name = resolve_dongle_process_name(
        {DONGLE_PROCESS_SETTING_KEY: process_name} if process_name else None
    )
    running = dongle_process_running(process_name)
    return {
        "process": process_name,
        "running": running,
        "checked": sys.platform.startswith("win"),
    }


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    unique = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _existing_files(paths: Iterable[Path]) -> list[Path]:
    return [path for path in _unique_paths(paths) if path.is_file()]


def _env_path(*names: str) -> list[Path]:
    paths = []
    for name in names:
        value = os.environ.get(name)
        if value:
            paths.append(Path(value))
    return paths


def _which_path(exe_name: str) -> list[Path]:
    found = shutil.which(exe_name)
    return [Path(found)] if found else []


def _drive_roots() -> list[Path]:
    roots = []
    for letter in "CDEFGHI":
        root = Path(f"{letter}:\\")
        if root.exists():
            roots.append(root)
    return roots


def _mapgis_install_dir_candidates() -> list[Path]:
    suffixes = (
        "mapgis67",
        "MapGIS67",
        "mapgis",
        "MapGIS",
        "MapGIS 6.7",
        "Program Files\\MapGIS67",
        "Program Files (x86)\\MapGIS67",
        "Program Files\\MapGIS",
        "Program Files (x86)\\MapGIS",
    )
    return [root / suffix for root in _drive_roots() for suffix in suffixes]


def program_candidates(program_name: str) -> list[Path]:
    exe_name = MAPGIS_PROGRAM_NAMES[program_name]
    candidates = []
    if program_name == "section":
        candidates.extend(_env_path("MAPGIS67_SECTION_EXE", "SECTION_EXE"))
    elif program_name == "w60_conv":
        candidates.extend(_env_path("MAPGIS67_W60_CONV_EXE", "W60_CONV_EXE"))
    candidates.extend(_which_path(exe_name))
    for install_dir in _mapgis_install_dir_candidates():
        candidates.append(install_dir / "program" / exe_name)
        candidates.append(install_dir / exe_name)
    return _unique_paths(candidates)


def _find_bootstrap_files(project_root: Path, limit: int = 20) -> list[Path]:
    candidates = []
    candidates.extend(_env_path("MAPGIS67_BOOTSTRAP_FILE", "SECTION_BOOTSTRAP_FILE"))
    candidates.append(DEFAULT_BOOTSTRAP_FILE)

    search_roots = [project_root]
    search_roots.extend(path for path in project_root.glob("T01_*") if path.is_dir())
    for root in search_roots:
        if not root.is_dir():
            continue
        for suffix in ("*.WT", "*.WL", "*.WP", "*.MPJ", "*.wt", "*.wl", "*.wp", "*.mpj"):
            for path in root.rglob(suffix):
                candidates.append(path)
                if len(candidates) >= limit:
                    break
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    return _existing_files(candidates)[:limit]


def _dependency_report(dependency_modules: Mapping[str, str]) -> dict[str, dict[str, object]]:
    report: dict[str, dict[str, object]] = {}
    for module_name, package_name in dependency_modules.items():
        try:
            spec = importlib.util.find_spec(module_name)
            available = spec is not None
        except (ImportError, AttributeError, ValueError):
            available = False
        report[module_name] = {"available": available, "install_hint": package_name}
    return report


def _dependency_available(report: Mapping[str, object], module_name: str) -> bool:
    python_report = report.get("python", {})
    if not isinstance(python_report, Mapping):
        return False
    dependencies = python_report.get("dependencies", {})
    if not isinstance(dependencies, Mapping):
        return False
    dependency = dependencies.get(module_name, {})
    return isinstance(dependency, Mapping) and bool(dependency.get("available"))


def _program_selected(report: Mapping[str, object], program_name: str) -> bool:
    programs = report.get("programs", {})
    if not isinstance(programs, Mapping):
        return False
    program = programs.get(program_name, {})
    return isinstance(program, Mapping) and bool(program.get("selected"))


def build_capability_summary(report: Mapping[str, object]) -> dict[str, object]:
    """Summarize probe output into agent-facing readiness decisions."""

    return {
        "stable_mcp_tools": [
            "probe_environment",
            "prepare_batch",
            "verify_batch",
            "collect_ready",
            "section_batch_convert",
            "run_dxf_to_wl_wt_pipeline",
        ],
        "local_capabilities": {
            "candidate_generation": {
                "ready": bool(report.get("ok_for_candidate_generation")),
                "requires": ["cv2", "numpy", "PIL"],
            },
            "mcp_server": {
                "ready": _dependency_available(report, "mcp.server.fastmcp"),
                "requires": ["mcp.server.fastmcp"],
            },
            "section_gui_automation": {
                "ready": bool(report.get("ok_for_section_gui_automation")),
                "requires": ["section.selected_exe", "bootstrap.selected_file", "win32gui"],
            },
            "w60_conversion_helper": {
                "ready": _program_selected(report, "w60_conv"),
                "requires": ["programs.w60_conv.selected"],
                "status": "discovered_only",
            },
            "mapgis_editing_program": {
                "ready": _program_selected(report, "mdiedit"),
                "requires": ["programs.mdiedit.selected"],
                "status": "discovered_only",
            },
        },
        "recommended_first_tools": [
            "probe_environment",
            "prepare_batch",
            "section_batch_convert",
            "verify_batch",
            "collect_ready",
        ],
        "boundaries": [
            "SECTION GUI automation still requires output-file verification",
            "W60/MapGIS helper discovery does not prove GUI automation is implemented",
            "No geological interpretation is automated by this probe",
        ],
    }


def build_environment_report(
    *,
    project_root: Path = PROJECT_ROOT,
    section_candidates: Sequence[Path] | None = None,
    program_candidate_map: Mapping[str, Sequence[Path]] | None = None,
    dependency_modules: Mapping[str, str] = DEFAULT_DEPENDENCIES,
) -> dict[str, object]:
    project_root = project_root.resolve()
    program_report: dict[str, dict[str, object]] = {}

    for key in MAPGIS_PROGRAM_NAMES:
        candidates = (
            list(program_candidate_map[key])
            if program_candidate_map is not None and key in program_candidate_map
            else program_candidates(key)
        )
        existing = _existing_files(candidates)
        program_report[key] = {
            "exe_name": MAPGIS_PROGRAM_NAMES[key],
            "selected": str(existing[0]) if existing else None,
            "found": [str(path) for path in existing],
            "checked_count": len(_unique_paths(candidates)),
        }

    if section_candidates is not None:
        existing_section = _existing_files(section_candidates)
        program_report["section"] = {
            "exe_name": MAPGIS_PROGRAM_NAMES["section"],
            "selected": str(existing_section[0]) if existing_section else None,
            "found": [str(path) for path in existing_section],
            "checked_count": len(_unique_paths(section_candidates)),
        }

    bootstrap_files = _find_bootstrap_files(project_root)
    dependencies = _dependency_report(dependency_modules)
    candidate_deps = ("cv2", "numpy", "PIL")
    automation_deps = ("win32gui",)

    selected_section = program_report["section"]["selected"]
    selected_bootstrap = str(bootstrap_files[0]) if bootstrap_files else None
    ok_for_candidates = all(dependencies.get(name, {}).get("available") for name in candidate_deps)
    ok_for_section_automation = bool(selected_section and selected_bootstrap) and all(
        dependencies.get(name, {}).get("available") for name in automation_deps
    )

    recommendations = []
    if not selected_section:
        recommendations.append("Set --section-exe or MAPGIS67_SECTION_EXE to the local section.exe path.")
    if not selected_bootstrap:
        recommendations.append("Set --bootstrap-file or MAPGIS67_BOOTSTRAP_FILE to any existing .WL/.WT/.WP/.MPJ file.")
    for module_name, info in dependencies.items():
        if not info["available"]:
            recommendations.append(f"Install Python package for {module_name}: pip install {info['install_hint']}")

    report: dict[str, object] = {
        "project_root": str(project_root),
        "python": {
            "executable": sys.executable,
            "version": sys.version,
            "dependencies": dependencies,
        },
        "programs": program_report,
        "section": {
            "selected_exe": selected_section,
            "can_pass_cli_arg": "--section-exe",
        },
        "bootstrap": {
            "selected_file": selected_bootstrap,
            "candidates": [str(path) for path in bootstrap_files],
            "can_pass_cli_arg": "--bootstrap-file",
        },
        "ok_for_candidate_generation": ok_for_candidates,
        "ok_for_section_gui_automation": ok_for_section_automation,
        "recommendations": recommendations,
    }
    report["capabilities"] = build_capability_summary(report)
    return report


def write_report(path: Path, report: Mapping[str, object]) -> None:
    from geoscan.candidates import write_json

    write_json(path, dict(report))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe local MapGIS67/SECTION and Python environment.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--write-json", type=Path, help="Write the probe report to a JSON file.")
    args = parser.parse_args(argv)

    report = build_environment_report(project_root=args.project_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.write_json:
        write_report(args.write_json, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
