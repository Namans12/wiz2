# Production Validation

Use this checklist before packaging, publishing, or asking external users to test.

## Automated Gate

Run from the repo root:

```powershell
python -m ruff check .
python -m pytest
```

Run Android checks:

```powershell
cd wizlight-android
flutter analyze
flutter test
flutter build apk --debug
flutter build apk --release
flutter build appbundle --release
```

Build the Windows package:

```powershell
cd ..
.\packaging\build_windows.ps1
```

## Desktop Manual Gate

- Launch `python -m src.gui`.
- Confirm no-bulb state disables light/sync/clap controls.
- Discover bulbs and remove stale bulbs.
- Test power, brightness, color picker, color temperature, and presets.
- Start screen sync, then verify `requested/current FPS`, `Frame ms`, `Send Hz`, `Motion`, `Capture`, target color, and output color update.
- Change Responsiveness, Smoothness, Color Punch, Dark Scene Floor, Monitor, and Fast GPU capture while sync is running.
- Confirm changed settings show `Restarting sync...` when restart is required.
- Export Diagnostics JSON from Tools and confirm it includes `screenSyncSettings` and `runtimeStats`.
- Test tray actions: toggle lights, screen sync, clap detection, brightness, color temperature, monitor, presets, exit.

## Android Manual Gate

- Install the debug or release APK on a real Android device.
- Confirm WiZ discovery and manual IP add.
- Confirm screen-capture permission flow starts the foreground service.
- Start/stop screen sync and watch FPS, send Hz, motion, frame time, boost, floor, delta, and capture backend.
- Change Max FPS, Smoothing, Color Boost, mode, and letterbox setting before starting sync.
- Export Diagnostics JSON and confirm the path shown in the status message exists on the device.

## Hardware Accuracy Gate

Create `hardware_targets.json` from the example, then run a dry run:

```powershell
Copy-Item hardware_targets.example.json hardware_targets.json
python scripts\run_hardware_color_accuracy.py --manifest hardware_targets.json
```

Run the real camera/bulb validation:

```powershell
python scripts\run_hardware_color_accuracy.py --manifest hardware_targets.json --execute
```

The hardware gate should pass the manifest thresholds for mean and max color error. Use `docs/hardware-color-accuracy.md` for camera ROI and calibration details.

## Network Requirements

WiZ local communication must be enabled in the WiZ app under home security settings. The official WiZ help center says disabling local communication prevents products from accepting local UDP messages and limits control to cloud communication.
