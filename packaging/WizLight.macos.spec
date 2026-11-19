# -*- mode: python ; coding: utf-8 -*-
# macOS PyInstaller spec for WizLight.
# Produces a .app bundle that is then packaged into a .dmg by build_macos.sh.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


root = Path.cwd()
sys.path.insert(0, str(root / "packaging"))
from version_tools import read_version  # noqa: E402

VERSION = read_version()  # single source: src/__init__.py
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
    "mss",                  # cross-platform fallback capture
    "sounddevice",
    "numpy",
    "Quartz",               # PyObjC binding for CG display queries (macOS)
    "AppKit",
    "CoreFoundation",
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
        "dxcam",            # Windows-only; explicitly excluded from macOS build
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WizLight",
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
    icon=str(root / "packaging" / "assets" / "wizlight.icns")
        if (root / "packaging" / "assets" / "wizlight.icns").exists()
        else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WizLight",
)

app = BUNDLE(
    coll,
    name="WizLight.app",
    icon=str(root / "packaging" / "assets" / "wizlight.icns")
        if (root / "packaging" / "assets" / "wizlight.icns").exists()
        else None,
    bundle_identifier="com.wizlight.desktop",
    version=VERSION,
    info_plist={
        "CFBundleName": "WizLight",
        "CFBundleDisplayName": "WizLight",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        # Microphone access for clap detection
        "NSMicrophoneUsageDescription":
            "WizLight uses the microphone to detect claps for hands-free bulb control.",
        # Screen capture for ambient sync (macOS 10.15+ requires this)
        "NSScreenCaptureDescription":
            "WizLight captures your screen to sync WiZ bulb colours with on-screen content.",
        "LSApplicationCategoryType": "public.app-category.utilities",
    },
)
