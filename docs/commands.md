# Command Reference

Run desktop commands from the repo root:

```powershell
cd C:\Users\naman\Desktop\wizlight-master
```

## Python Setup

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e ".[dev,calibration,package]"
```

If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
```

## Desktop GUI

```powershell
python -m src.gui
```

Installed console command:

```powershell
wizlight gui
```

Built executable:

```powershell
.\dist\WizLight\WizLight.exe
```

## CLI

```powershell
wizlight --help
wizlight discover
wizlight status
wizlight on
wizlight off
wizlight toggle
wizlight brightness 80
wizlight color 255 80 20
wizlight temp 4000
wizlight preset warm
wizlight preset cool
wizlight preset night
```

Most bulb-control commands accept `-i IP` to target one bulb.

Screen sync:

```powershell
wizlight sync-screen
python scripts\sync_screen.py
```

## Scripts

```powershell
python scripts\network_diagnostic.py
python scripts\diagnose_bulb.py
python scripts\list_mics.py
python scripts\debug_mic.py
python scripts\test_clap.py
python scripts\clap_toggle.py
python scripts\color_preset.py
python scripts\calibrate_bulb_camera.py
python scripts\run_hardware_color_accuracy.py
python scripts\benchmark_screen_sync.py --iterations 200 --width 1920 --height 1080
python scripts\benchmark_desktop_navigation.py --iterations 60
python scripts\measure_control_effects.py
python scripts\fake_wiz_bulb_udp.py --host 127.0.0.1 --port 38899 --seconds 20
```

The desktop navigation benchmark opens a temporary test window and reports
synthetic page-switch timing. It does not capture the screen or control lights.

Hardware color verification:

```powershell
python scripts\verify_video_color_mapping.py C:\path\to\video.mp4 --ip 192.168.1.3 --camera 0 --roi 120,80,90,90
```

Hardware test manifest flow:

```powershell
Copy-Item hardware_targets.example.json hardware_targets.json
notepad hardware_targets.json
python scripts\run_hardware_color_accuracy.py --manifest hardware_targets.json
python scripts\run_hardware_color_accuracy.py --manifest hardware_targets.json --execute
```

## Tests And Lint

```powershell
python -m ruff check .
python -m pytest
```

Hardware tests:

```powershell
$env:RUN_HARDWARE_TESTS = "1"
python -m pytest -m hardware
```

## Windows Installer

```powershell
.\packaging\build_windows.ps1
```

Outputs:

```text
dist\WizLight\WizLight.exe   (onedir folder; keep it together)
dist\WizLight-portable.zip   (unzip anywhere and run WizLight.exe)
dist\installer\WizLight-Setup.exe
```

The installer (and the portable zip) unpack the onedir build, so launches no longer extract
to a temp folder. Installing over an older install replaces the old files; close WizLight first
(the installer and uninstaller refuse to run while it is open). Uninstall removes the whole
install folder plus the shortcuts.

Run installer:

```powershell
.\dist\installer\WizLight-Setup.exe
```

## Android

```powershell
cd C:\Users\naman\Desktop\wizlight-master\wizlight-android
flutter pub get
flutter analyze
flutter test
```

Run on connected phone/emulator:

```powershell
flutter devices
flutter run
```

Build APKs and app bundle:

```powershell
flutter build apk --debug
flutter build apk --release
flutter build appbundle --release
```

Outputs:

```text
wizlight-android\build\app\outputs\flutter-apk\app-debug.apk
wizlight-android\build\app\outputs\flutter-apk\app-release.apk
wizlight-android\build\app\outputs\bundle\release\app-release.aab
```

Install debug APK:

```powershell
adb install .\build\app\outputs\flutter-apk\app-debug.apk
```

## Clean Generated Files

Desktop:

```powershell
Remove-Item -Recurse -Force .pytest_cache,.ruff_cache,wizlight.egg-info -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Remove-Item -Recurse -Force build,dist -ErrorAction SilentlyContinue
```

Android:

```powershell
cd C:\Users\naman\Desktop\wizlight-master\wizlight-android
flutter clean
flutter pub get
```

## Recommended Daily Flow

```powershell
cd C:\Users\naman\Desktop\wizlight-master
.\.venv\Scripts\Activate.ps1
python -m ruff check .
python -m pytest
python -m src.gui
```
