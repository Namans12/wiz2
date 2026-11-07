# -*- mode: python ; coding: utf-8 -*-
# Console diagnostic build mirroring WizLight.spec bundling, used to find
# frozen-environment failures that don't occur when running from source.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


root = Path.cwd()
datas = []
datas += collect_data_files("customtkinter")

hiddenimports = []
for package in (
    "PIL",
    "customtkinter",
    "pystray",
    "pywizlight",
):
    hiddenimports += collect_submodules(package)

hiddenimports += [
    "dxcam",
    "mss",
    "sounddevice",
    "numpy",
]


a = Analysis(
    [str(root / "packaging" / "frozen_diag.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "tests", "cv2", "matplotlib", "IPython", "jupyter", "fauxmo"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FrozenDiag",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
