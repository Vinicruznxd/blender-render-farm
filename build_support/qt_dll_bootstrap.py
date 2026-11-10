"""Load the bundled Qt runtime before application imports on Windows.

PyInstaller normally configures the Qt search path. Preloading by absolute
path also protects portable builds from stale Qt DLLs left by an older bundle
or injected through PATH by Blender and other DCC applications.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import traceback


_QT_DLL_DIRECTORY_HANDLE = None
_QT_DLL_HANDLES: list[object] = []


def _bootstrap_bundled_qt() -> None:
    global _QT_DLL_DIRECTORY_HANDLE

    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    qt_dir = bundle_root / "PySide6"
    if not qt_dir.is_dir():
        return

    # Keep the AddDllDirectory cookie alive for the entire process.
    if hasattr(os, "add_dll_directory"):
        _QT_DLL_DIRECTORY_HANDLE = os.add_dll_directory(str(qt_dir))

    os.environ["PATH"] = str(qt_dir) + os.pathsep + os.environ.get("PATH", "")

    # LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS.
    load_flags = 0x00000100 | 0x00001000
    for dll_name in ("Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll"):
        dll_path = qt_dir / dll_name
        if dll_path.exists():
            _QT_DLL_HANDLES.append(ctypes.WinDLL(str(dll_path), winmode=load_flags))


try:
    _bootstrap_bundled_qt()
except BaseException:
    try:
        diagnostic_path = Path(sys.executable).resolve().parent / "qt_startup_error.txt"
        diagnostic_path.write_text(traceback.format_exc(), encoding="utf-8")
    except BaseException:
        pass
    raise
