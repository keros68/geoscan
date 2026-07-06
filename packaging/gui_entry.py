"""PyInstaller entry point for the frozen GeoScan engine executable.

``GeoScan.exe --engine`` -> JSONL engine host on stdio (spawned by the
Tauri console shell ``GeoScanConsole.exe``; same as
``python -m geoscan.engine_host``)
``GeoScan.exe --check``  -> headless startup check (build smoke test)
``GeoScan.exe --batch ...`` -> command-line batch runner (same args
as ``python -m geoscan.batch_runner run``)

GeoScan.exe has no UI of its own (the classic tkinter GUI was removed):
run with no arguments (double-click) it hands over to the sibling
``GeoScanConsole.exe``.
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
    argv = sys.argv[1:]
    if argv and argv[0] == "--engine":
        # JSONL engine host for the Tauri console. stdio IS the protocol, so it
        # must not be nulled: the shell provides pipe handles even though this
        # is a windowed build. Launched without a usable peer (double-click, or
        # a caller that didn't wire pipes) the std streams may be None OR bound
        # to an invalid handle that only fails on flush ([Errno 22]) — probe
        # with a real flush and exit quietly instead of crashing on `hello`.
        try:
            if sys.stdout is None or sys.stdin is None:
                return 2
            sys.stdout.write("")
            sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            return 2
        engine = _engine_dir()
        if engine is not None:
            sys.path.insert(0, str(engine))
        from geoscan.engine_host import main as engine_main

        return engine_main()

    _silence_missing_std_streams()
    engine = _engine_dir()
    if engine is not None:
        sys.path.insert(0, str(engine))  # loose engine wins over anything else
    if argv and argv[0] in {"--help", "-h"}:
        print(
            "\n".join(
                [
                    "Usage:",
                    "  GeoScan.exe --engine",
                    "  GeoScan.exe --check",
                    "  GeoScan.exe --batch --project-root <workdir> --source-dir <tiff-folder> [options]",
                    "",
                    "GeoScan.exe is the engine executable and has no UI of its own;",
                    "the user interface is GeoScanConsole.exe. Run with no arguments",
                    "it launches the sibling GeoScanConsole.exe if present.",
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

        # Exercise the compiled cv2 extension for real. A broken bundle (or a
        # stale cv2/ left over from an older install) can still satisfy
        # `import cv2` — as an empty namespace package or a mismatched wrapper
        # — so an actual conversion call is the only trustworthy probe. This
        # runs both as the build smoke test and as the installer's post-install
        # self-check.
        import numpy as _np
        import cv2 as _cv2

        _gray = _cv2.cvtColor(_np.zeros((4, 4, 3), dtype=_np.uint8), _cv2.COLOR_BGR2GRAY)
        if _gray.shape != (4, 4):
            raise RuntimeError("cv2 self-check returned wrong shape")

        bootstrap_settings()
        # Import the real app modules the console engine runs on — proves the
        # loose engine layer resolves end to end without spinning up any UI.
        from geoscan import engine_host, run_form

        for _module in (engine_host, run_form):
            if not getattr(_module, "__file__", None):
                raise RuntimeError(f"{_module.__name__} did not resolve to a file")

        # Report where geoscan loaded from — confirms the loose engine layer is
        # active (path under .../engine/geoscan) and its version.
        print(f"startup check passed (geoscan {geoscan.__version__} @ {geoscan.__file__})")
        return 0
    if argv and argv[0] == "--batch":
        from geoscan.batch_runner import main as batch_main

        return batch_main(["run", *argv[1:]])

    # No-args (double-click): the classic tkinter GUI is gone — hand over to
    # the console shell installed next to this exe.
    if getattr(sys, "frozen", False):
        console = Path(sys.executable).resolve().parent / "GeoScanConsole.exe"
        if console.is_file():
            import subprocess

            subprocess.Popen(
                [str(console)],
                cwd=str(console.parent),
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            return 0
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        None,
        "GeoScan 的界面是 GeoScanConsole.exe（控制台），请从开始菜单或桌面的"
        " GeoScan 快捷方式启动。\n\n"
        "GeoScan.exe 本身没有界面，只作为引擎使用（--engine / --batch / --check）。",
        "GeoScan",
        0x30,  # MB_ICONWARNING
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
