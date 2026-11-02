# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the pgm2mc onedir bundle. Run via packaging/macos/build.sh, not directly.
# Output: dist/pgm2mc/pgm2mc (executable) + dist/pgm2mc/_internal/ (payload).
import os

from PyInstaller.utils.hooks import collect_submodules

REPO = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
ENGINE = os.path.join(REPO, "engine")

# every engine module, including pgm2mc.__main__ (run by the launcher via runpy) and the optional
# supersource module that analyze.py imports lazily
hidden = collect_submodules("pgm2mc", filter=lambda name: True) + ["pgm2mc.__main__"]

a = Analysis(
    [os.path.join(SPECPATH, "pgm2mc_launcher.py")],
    pathex=[ENGINE],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "_tkinter", "test"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pgm2mc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=os.environ.get("PGM2MC_TARGET_ARCH", "arm64"),
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="pgm2mc",
)
