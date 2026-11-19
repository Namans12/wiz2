#!/usr/bin/env bash
# Build the WizLight macOS .app bundle and wrap it in a .dmg installer.
#
# Run on macOS only (PyInstaller needs the target platform's runtime).
# Prereqs:
#   * Python 3.10+ (recommend pyenv or python.org installer)
#   * pip install -r requirements-dev.txt   (includes pyinstaller and the pyobjc packages)
#   * (optional, for .dmg)  brew install create-dmg
#
# Usage:
#   bash packaging/build_macos.sh
# Output:
#   dist/WizLight.app
#   dist/WizLight-<version>-macOS.dmg   (only if create-dmg is on PATH)

set -euo pipefail
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: Build the macOS app on a macOS host." >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"

build_marker="$(mktemp)"
trap 'rm -f "$build_marker"' EXIT

echo "==> Ensuring icon assets exist"
"$PYTHON" packaging/make_icon.py

# If wizlight.icns is missing but a PNG is present, attempt to synthesise one
# using iconutil + sips. Skip silently if either tool is unavailable.
if [[ ! -f packaging/assets/wizlight.icns ]] && [[ -f packaging/assets/wizlight_icon.png ]]; then
    if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
        echo "==> Generating .icns from PNG"
        tmpdir="$(mktemp -d)/wizlight.iconset"
        mkdir -p "$tmpdir"
        for size in 16 32 64 128 256 512; do
            sips -z "$size" "$size" packaging/assets/wizlight_icon.png \
                --out "$tmpdir/icon_${size}x${size}.png" >/dev/null
            sips -z $((size * 2)) $((size * 2)) packaging/assets/wizlight_icon.png \
                --out "$tmpdir/icon_${size}x${size}@2x.png" >/dev/null
        done
        iconutil -c icns "$tmpdir" -o packaging/assets/wizlight.icns
    else
        echo "    (skipping .icns generation — iconutil/sips not available)"
    fi
fi

echo "==> Building app bundle via PyInstaller"
"$PYTHON" -m PyInstaller --clean --noconfirm packaging/WizLight.macos.spec

APP_PATH="dist/WizLight.app"
if [[ ! -f "$APP_PATH/Contents/MacOS/WizLight" ]] || [[ ! "$APP_PATH/Contents/MacOS/WizLight" -nt "$build_marker" ]]; then
    echo "ERROR: $APP_PATH was not produced. Check PyInstaller log above." >&2
    exit 1
fi

echo "==> App bundle ready: $APP_PATH"

if command -v create-dmg >/dev/null 2>&1; then
    VERSION="$("$PYTHON" packaging/version_tools.py --print)"
    DMG_PATH="dist/WizLight-${VERSION}-macOS.dmg"
    echo "==> Building DMG: $DMG_PATH"
    rm -f "$DMG_PATH"
    # On CI there is no Finder session for create-dmg's AppleScript layout step; skip it there.
    create-dmg ${CI:+--skip-jenkins} \
        --volname "WizLight" \
        --window-pos 200 120 \
        --window-size 600 320 \
        --icon-size 96 \
        --icon "WizLight.app" 160 140 \
        --app-drop-link 440 140 \
        --hide-extension "WizLight.app" \
        "$DMG_PATH" \
        "$APP_PATH"
    echo "==> DMG ready: $DMG_PATH"
else
    echo "==> Skipping DMG step (install with: brew install create-dmg)"
fi

echo "==> Done (unsigned; signing and notarization are separate release steps)."
