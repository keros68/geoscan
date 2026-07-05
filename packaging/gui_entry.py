"""PyInstaller entry point for the standalone MapGIS semi-auto vectorization GUI.

``GeoScan.exe``          -> starts the GUI
``GeoScan.exe --check``  -> headless startup check (build smoke test)
``GeoScan.exe --batch ...`` -> command-line batch runner (same args
as ``python -m geoscan.batch_runner run``)
"""

from __future__ import annotations

import os
import sys


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
        from geoscan.app_settings import bootstrap_settings
        from geoscan.production_gui import ProductionGui

        bootstrap_settings()
        app = ProductionGui()
        app.destroy()
        print("GUI startup check passed")
        return 0
    if argv and argv[0] == "--batch":
        from geoscan.batch_runner import main as batch_main

        return batch_main(["run", *argv[1:]])

    from geoscan.production_gui import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
