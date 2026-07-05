"""PyInstaller entry point for the standalone MapGIS semi-auto vectorization GUI.

``GeoScan.exe``          -> starts the GUI
``GeoScan.exe --check``  -> headless startup check (build smoke test)
``GeoScan.exe --batch ...`` -> command-line batch runner (same args
as ``python -m geoscan.batch_runner run``)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _engine_dir() -> Path | None:
    """The loose engine layer shipped next to the frozen runtime.

    Two-layer update model: the `geoscan` package lives as loose source in
    `<_internal>/engine/`, so a code update only replaces that ~1 MB folder.
    Returns the dir to put on sys.path[0], or None when running from source
    (dev), where the installed/editable `geoscan` is used as-is.
    """
    if not getattr(sys, "frozen", False):
        return None
    base = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).resolve().parent)
    candidate = base / "engine"
    if (candidate / "geoscan" / "__init__.py").is_file():
        return candidate
    return None


def _silence_missing_std_streams() -> None:
    """Route stdout/stderr to null when they are absent.

    A windowed (console=False) PyInstaller build has ``sys.stdout``/``stderr``
    set to None, so any ``print()`` (in --check/--help/--batch or a library)
    would raise. Send them to the null device instead. GUI users read the
    on-screen log pane; there is no console to show anyway.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))


def main() -> int:
    _silence_missing_std_streams()
    engine = _engine_dir()
    if engine is not None:
        sys.path.insert(0, str(engine))  # loose engine wins over anything else
    argv = sys.argv[1:]
    if argv and argv[0] in {"--help", "-h"}:
        print(
            "\n".join(
                [
                    "Usage:",
                    "  GeoScan.exe",
                    "  GeoScan.exe --check",
                    "  GeoScan.exe --batch --project-root <workdir> --source-dir <tiff-folder> [options]",
                    "",
                    "Batch options are the same as:",
                    "  python -m geoscan.batch_runner run --help",
                ]
            )
        )
        return 0
    if argv and argv[0] == "--check":
        import geoscan
        from geoscan.app_settings import bootstrap_settings
        from geoscan.production_gui import ProductionGui

        bootstrap_settings()
        app = ProductionGui()
        app.destroy()
        # Report where geoscan loaded from — confirms the loose engine layer is
        # active (path under .../engine/geoscan) and its version.
        print(f"GUI startup check passed (geoscan {geoscan.__version__} @ {geoscan.__file__})")
        return 0
    if argv and argv[0] == "--batch":
        from geoscan.batch_runner import main as batch_main

        return batch_main(["run", *argv[1:]])

    from geoscan.production_gui import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
