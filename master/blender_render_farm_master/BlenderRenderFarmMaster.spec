# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path.cwd()
shared_root = project_root.parents[1] / "shared"
icon_path = project_root / "img" / "icon_master.ico"
build_support_dir = project_root.parents[1] / "build_support"

license_datas = [
    (str(project_root.parents[1] / name), "licenses")
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")
] + [
    (str(project_root.parents[1] / "LICENSES"), "licenses/third_party"),
    (str(project_root.parents[1] / "docs" / "LICENSING.md"), "licenses"),
]

a = Analysis(
    ["main.py"],
    pathex=[str(project_root), str(shared_root)],
    binaries=[],
    datas=license_datas + [
        (str(project_root / "config.json"), "."),
        (str(project_root / "img"), "img"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(build_support_dir / "qt_dll_bootstrap.py")],
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
    name="BlenderRenderFarmMaster",
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
    name="BlenderRenderFarmMaster",
)
