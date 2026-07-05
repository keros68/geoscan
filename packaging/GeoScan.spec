# PyInstaller spec for the standalone MapGIS semi-auto vectorization GUI.
# Build from the repo root:
#   pyinstaller packaging/GeoScan.spec --noconfirm
# Output: dist/GeoScan/ (one-folder; copy the whole folder).

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_dynamic_libs,
    collect_submodules,
)

# SPECPATH is this spec's own directory (…/geoscan/packaging), so the repo root
# is one level up. (Was .parent.parent under the old, more deeply-nested layout.)
repo_root = Path(SPECPATH).resolve().parent

# The package lives under src/ (src-layout). Put it on sys.path so both
# collect_submodules below and PyInstaller's analysis can import `geoscan`.
sys.path.insert(0, str(repo_root / "src"))

# Private, local-only module tier (git-ignored, never published). The public
# mainline does not import these, so they are NOT bundled — this keeps the
# shipped installer, which becomes a public GitHub release asset, free of any
# private code. Keep in step with .git/info/exclude.
PRIVATE_MODULES = {
    "native_direct", "wl_from_scratch", "wt_w60_derived", "wt_from_seed",
    "mapgis_binary", "mapgis_wl", "mapgis_wt", "mapgis_wp", "native_format_lab",
    "wt_native_diagnostics", "mapgis67_diagnostics",
}


def _is_public(module_name):
    leaf = module_name.split(".")[-1]
    return leaf not in PRIVATE_MODULES and "seed_templates" not in module_name

# Bundle the OCR engine (rapidocr ships its .onnx models inside the wheel, so
# colleague machines need no Python, no conda env and no internet). The
# in-process OCR route in text_candidate_workflow picks it up automatically.
rapidocr_datas, rapidocr_binaries, rapidocr_hidden = collect_all("rapidocr")
ort_datas, ort_binaries, ort_hidden = collect_all("onnxruntime")
svttk_datas, svttk_binaries, svttk_hidden = collect_all("sv_ttk")
# opencv (cv2): opencv 4.13's __init__.py imports the native cv2.pyd through a
# runtime bootstrap that PyInstaller's static analysis cannot see, and .pyd is
# not in collect_dynamic_libs' default patterns -> the extension is dropped and
# `import cv2` succeeds but `cv2.cvtColor` (every real symbol) is absent,
# crashing every run that touches leveling/enhance/line extraction. collect_all
# gets the data files + ffmpeg DLL + hidden submodules; we additionally force
# the .pyd extension in via an explicit *.pyd search pattern (portable — reads
# the installed cv2's real location, no hardcoded path).
cv2_datas, _cv2_all_binaries, cv2_hidden = collect_all("cv2")
cv2_binaries = collect_dynamic_libs("cv2", search_patterns=["*.dll", "*.pyd"])

# Ship the package .py sources inside _internal/py_src/ so the external OCR
# interpreter (settings key "ocr_python") can run
# `python -m geoscan.ocr_subprocess` with
# PYTHONPATH=_internal/py_src. A dedicated subfolder is REQUIRED: putting
# _internal itself on the child's PYTHONPATH would make it import the frozen
# build's compiled numpy/cv2 (wrong Python version) and break rapidocr.
package_source_datas = [
    (str(path), "py_src/geoscan")
    for path in (repo_root / "src" / "geoscan").glob("*.py")
    if path.stem not in PRIVATE_MODULES
]

a = Analysis(
    [str(repo_root / "packaging" / "gui_entry.py")],
    pathex=[str(repo_root / "src"), str(repo_root)],
    binaries=[*rapidocr_binaries, *ort_binaries, *cv2_binaries],
    datas=[
        (
            str(repo_root / "src" / "geoscan" / "section_bootstrap" / "SECTION_BOOTSTRAP.WT"),
            "geoscan/section_bootstrap",
        ),
        (
            str(repo_root / "packaging" / "mapgis_settings.example.json"),
            ".",
        ),
        (
            str(repo_root / "packaging" / "app_icon.ico"),
            ".",
        ),
        *package_source_datas,
        *rapidocr_datas,
        *ort_datas,
        *svttk_datas,
        *cv2_datas,
    ],
    hiddenimports=[
        # Collect every PUBLIC geoscan submodule so function-level and string
        # imports are covered regardless of the static import graph. Private
        # modules are filtered out so they never enter the shipped installer.
        *[m for m in collect_submodules("geoscan") if _is_public(m)],
        "PIL._tkinter_finder",
        "sv_ttk",
        *rapidocr_hidden,
        *ort_hidden,
        *svttk_hidden,
        *cv2_hidden,
    ],
    hookspath=[],
    runtime_hooks=[],
    # Heavy libs the production package never imports (verified by grep) but that
    # a polluted dev env drags in transitively: PyMatting -> numba/llvmlite,
    # scikit-image/scikit-learn -> scipy, seaborn -> pandas. Excluding them (plus
    # dev-only IPython/jedi/psycopg2) trims ~200 MB with zero code impact. The
    # real fix is building in a clean venv (release/build_clean.ps1); this is the
    # belt-and-suspenders backstop.
    excludes=[
        "pytest", "matplotlib", "torch", "tensorflow",
        "scipy", "numba", "llvmlite", "pandas",
        "pymatting", "skimage", "sklearn", "seaborn",
        "IPython", "jedi", "psycopg2",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GeoScan",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # windowed app: no cmd window on launch (GUI has its own log pane)
    icon=str(repo_root / "packaging" / "app_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="GeoScan",
)
