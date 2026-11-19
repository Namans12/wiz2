---
name: wizlight-build
description: Build, tune, verify, and package the WizLight apps (Flutter/Kotlin Android APK + Python/PyInstaller desktop for Windows & macOS). Use when changing color-sync code or producing installable artifacts.
---

# SKILL.md — wizlight-build
# Read this file completely before writing any code, creating any file, or running any command.

## What this skill covers
WizLight is a smart-bulb controller with low-latency screen-sync ambient lighting.
It has two shippable apps: a **Flutter + Kotlin Android app** (`wizlight-android/`,
native capture in `ScreenCaptureService.kt`) and a **Python desktop app** (`src/`,
CustomTkinter GUI + CLI) packaged with PyInstaller for Windows (`.exe`) and macOS
(`.app`/`.dmg`). This skill governs the loop of: change color-sync / capture code →
build the artifact → verify it → update state → report the artifact path. The
color pipeline (gamut auto-detect, CCT-based cold/warm white split, per-HDR-type
tone curves, OLED true-black, FP16 HDR capture) is the heart of the project.

## Environment constraints
- OS: Windows 11. Shell: PowerShell (primary) + Git Bash. Forward-slash paths work in Bash; use `C:\...` in PowerShell.
- Python: `python` on PATH (3.10+). Install with `pip install -r requirements.txt` (no `--break-system-packages` — that's Linux-only).
- Flutter: `C:\Users\naman\Downloads\flutter\bin\flutter` (3.38.9, Dart 3.10.8). Android SDK 36.1.0 present.
- Repo root: `C:\Users\naman\Desktop\wizlight-master`
- Android build outputs: `wizlight-android\build\app\outputs\flutter-apk\app-release.apk`
- Desktop build outputs (Windows): `dist\WizLight\WizLight.exe` (onedir) + `dist\WizLight-portable.zip`, `dist\installer\WizLight-Setup.exe`
- Desktop build outputs (macOS, build ON macOS only): `dist/WizLight.app`, `dist/WizLight-macOS.dmg`
- State file: `.claude\skills\state\state.json`
- Checkpoint log: `.claude\skills\state\checkpoint.log`

## Libraries / toolchain to use
- Android: Flutter `flutter build apk --release`; Gradle (assembleRelease) invoked by Flutter. Kotlin service uses MediaProjection, ImageReader, org.json.
- Desktop: `customtkinter`, `pywizlight`, `Pillow`, `numpy`, `mss` (cross-platform capture), `dxcam` (Windows-only, gated by `sys_platform=='win32'`), `sounddevice`, `pyobjc-*` (macOS gamut detect, gated by `sys_platform=='darwin'`).
- Packaging: `pyinstaller` + `packaging\WizLight.spec` (Windows) / `packaging\WizLight.macos.spec` (macOS) / `packaging\build_macos.sh`.

## Step-by-step workflow
Follow in order. Write a checkpoint (AGENT.md) after each step.

1. Read this SKILL.md completely (you are here)
2. Read AGENT.md at `.claude\skills\AGENT.md`
3. Read MEMORY.md at `.claude\skills\MEMORY.md` and load `state.json`
4. **Make the change** — edit Kotlin (`wizlight-android/.../ScreenCaptureService.kt`), Dart (`lib/`), or Python (`src/`). Keep desktop + Android color logic in parity (CCT split, profiles, gates).
5. **Build the affected artifact** — Android: `flutter pub get` then `flutter build apk --release`. Desktop (Win): `powershell packaging\build_windows.ps1`. Desktop (mac): `bash packaging/build_macos.sh` (macOS host only).
6. **Self-verify** — `flutter doctor` clean; Python `ast.parse` on every edited `.py`; confirm artifact exists and size is plausible (APK ≈ 40–60 MB).
7. Read CHECKS.md at `.claude\skills\CHECKS.md` and run all checks
8. Write updated state back to `state.json` (schema in MEMORY.md)
9. Confirm the final artifact path on disk
10. Report the artifact path + install instructions to the user

## Output rules
- Artifact naming: keep Flutter/PyInstaller defaults (`app-release.apk`, `WizLight.exe` (inside onedir folder `dist\WizLight\`), `WizLight.app`). Do not rename build outputs.
- A "release" report must include: artifact absolute path, size, build timestamp, and install steps for the target (Bravia/Google TV via adb, phone via adb/sideload, desktop via installer).
- Code edits: match surrounding style; keep Android Kotlin and Python `color_mapping.py` color math in sync.

## Known failure modes and fixes
- `cd` to a `C:\...` path in Bash fails ("No such file or directory"): use PowerShell, or use forward slashes / `Set-Location` in PowerShell. Prefer PowerShell for Flutter/Gradle.
- Gradle daemon / lock errors: re-run the build once; if it persists, `flutter clean` then rebuild.
- FP16 HDR capture path is API 34+ and reflection-based: never assume it engaged — read the Diagnostics panel (`captureBufferFormat`) to confirm; it silently falls back to `rgba8888`.
- macOS build cannot run on this Windows host — never claim a `.app`/`.dmg` was produced here; only validate the spec/script and instruct the user to run it on macOS.
- If any command fails: retry once (AGENT.md retry rules), then stop and explain the actual error output.

## Cross-references
After Step 9, read MEMORY.md and update `state.json`.
On any error in Steps 4–6, read AGENT.md for retry and escalation rules.
