# WizLight — macOS build

This directory contains the macOS-specific PyInstaller spec and build script.
Building must be done **on macOS** — PyInstaller bundles the running platform's
Python runtime, so cross-compiling from Windows or Linux does not work.

## Prerequisites

1. macOS 11 (Big Sur) or later.
2. Python 3.10+ (recommend [pyenv](https://github.com/pyenv/pyenv) or the
   official installer from python.org).
3. Install build dependencies:

   ```sh
   pip install -r requirements.txt
   pip install pyinstaller \
               pyobjc-core \
               pyobjc-framework-Cocoa \
               pyobjc-framework-Quartz
   ```

4. (Optional, for a `.dmg` installer) install `create-dmg`:

   ```sh
   brew install create-dmg
   ```

## Build

```sh
bash packaging/build_macos.sh
```

Outputs:

* `dist/WizLight.app` — Cocoa app bundle, drag-installable.
* `dist/WizLight-macOS.dmg` — installer disk image (only if `create-dmg`
  is on `PATH`).

## Codesigning & notarisation

The `build_macos.sh` script does **not** codesign or notarise the build — that
requires an Apple Developer ID certificate (`$DEVELOPER_ID_APPLICATION`) and
the `notarytool` CLI. After the bundle is built, sign + notarise with:

```sh
codesign --deep --force --options runtime \
    --sign "$DEVELOPER_ID_APPLICATION" \
    dist/WizLight.app
xcrun notarytool submit dist/WizLight-macOS.dmg \
    --apple-id "$APPLE_ID" \
    --team-id "$TEAM_ID" \
    --password "$APP_SPECIFIC_PWD" \
    --wait
xcrun stapler staple dist/WizLight-macOS.dmg
```

If you skip signing, users will see a "developer cannot be verified" warning
the first time they launch — they can right-click → Open to bypass it.

## What's optimised for Mac

The Python desktop now auto-detects the display gamut on launch via Quartz
(`CGDisplayCopyColorSpace`). On macOS this typically resolves to:

* **Display P3** — most retina Macs since 2016 (MacBook Pro, iMac, Studio
  Display).
* **Rec.2020** — Pro Display XDR when running in HDR mode.

When detected, the `device_profile` is set to `"mac"` and the LUT is
rebuilt with:

* +0.05 cool-mix bias (compensates for the slightly warm tone of Apple's
  reference white).
* CCT-based McCamy cool/warm split (same as the Bravia path).

You can override the auto choice from the desktop GUI's Color Source dropdown
if needed.

## Capture backend on macOS

DXCam (GPU capture) is Windows-only. On macOS the app falls back to MSS,
which uses Quartz's `CGDisplayCreateImage`. Performance is good for ambient
sync (~30 FPS at 1080p), but if you have a 5K/6K display you may want to
lower `max_fps` in the GUI to keep CPU usage down.

Screen recording permission is required — on first run, macOS will prompt
the user to grant it under **System Settings → Privacy & Security →
Screen Recording**. Restart the app after granting.
