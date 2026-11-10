# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project_root = Path.cwd()
shared_root = project_root.parents[1] / "shared"
icon_path = project_root / "img" / "icon_worker.ico"
hiddenimports = collect_submodules("uvicorn") + [
    "tkinter",
    "tkinter.ttk",
]

license_datas = [
    (str(project_root.parents[1] / name), "licenses")
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")
] + [
    (str(project_root.parents[1] / "LICENSES"), "licenses/third_party"),
    (str(project_root.parents[1] / "docs" / "LICENSING.md"), "licenses"),
]

a = Analysis(
    ["worker.py"],
    pathex=[str(project_root), str(shared_root)],
    binaries=[],
    datas=license_datas + [
        (str(project_root / "config.json"), "."),
        (str(project_root / "img"), "img"),
        (str(project_root / "blender_scripts"), "blender_scripts"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BlenderRenderFarmWorker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BlenderRenderFarmWorker",
)
