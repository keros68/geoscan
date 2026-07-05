"""PyInstaller entry point for the standalone MapGIS semi-auto vectorization GUI.

``mapgis_vectorize_gui.exe``          -> starts the GUI
``mapgis_vectorize_gui.exe --check``  -> headless startup check (build smoke test)
``mapgis_vectorize_gui.exe --batch ...`` -> command-line batch runner (same args
as ``python -m geoscan.batch_runner run``)
"""

from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in {"--help", "-h"}:
        print(
            "\n".join(
                [
                    "Usage:",
                    "  mapgis_vectorize_gui.exe",
                    "  mapgis_vectorize_gui.exe --check",
                    "  mapgis_vectorize_gui.exe --batch --project-root <workdir> --source-dir <tiff-folder> [options]",
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
