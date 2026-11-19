# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


root = Path.cwd()
datas = [
    (str(root / "README.md"), "."),
    (str(root / "LICENSE"), "."),
    (str(root / "packaging" / "assets"), "packaging/assets"),
]
datas += collect_data_files("customtkinter")

hiddenimports = []
for package in (
    "PIL",
    "customtkinter",
    "pystray",
    "pywizlight",
):
    hiddenimports += collect_submodules(package)

optional_hiddenimports = [
    "dxcam",
    "winrt.windows.graphics.capture.interop",
    "winrt.windows.graphics.directx.direct3d11.interop",
    "mss",
    "sounddevice",
    "numpy",
]
hiddenimports += optional_hiddenimports


a = Analysis(
    [str(root / "packaging" / "wizlight_gui_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "tests",
        "cv2",
        "matplotlib",
        "IPython",
        "jupyter",
    ],
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
    name="WizLight",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "packaging" / "assets" / "wizlight.ico"),
)
