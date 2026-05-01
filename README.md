# WizLight

Production-ready WiZ smart bulb controller for Windows desktop and Android. It focuses on fast local control, low-latency screen sync, clap detection, bulb calibration, and a clean GUI for day-to-day use.

The project controls WiZ bulbs locally over UDP. Your bulbs and controlling device must be on the same trusted local network, and local communication must be allowed in the WiZ app.

## Highlights

- Desktop GUI with bulb discovery, power toggle, brightness, RGB color picker, color temperature, presets, screen sync, clap detection, dashboard, and system tray controls.
- Real-time screen sync tuned for YouTube, Chrome, movies, and games using DXCam when available and MSS fallback otherwise.
- Fast UDP sender for latest-wins color packets, capped to a bulb-friendly send rate.
- Bulb-aware color mapping with RGB plus explicit white-channel handling for WiZ full-color bulbs.
- Camera-based calibration and opt-in real-bulb color accuracy tests.
- Clap detection with adaptive room-noise tracking and double-clap mode.
- CLI commands and scripts for automation, diagnostics, calibration, and testing.
- Android Flutter app for direct tablet/phone screen sync to WiZ bulbs.
- Optional Alexa bridge through fauxmo.

## Requirements

- Python 3.10+
- Windows recommended for the modern GUI and DXCam GPU capture
- WiZ bulbs on the same LAN as the desktop or Android device
- WiZ app setup completed
- WiZ app setting: allow local communication
- UDP `38899` and `38900` allowed on the local network
- Optional: microphone permission for clap detection
- Optional: OpenCV camera for real-bulb calibration
- Optional: Flutter SDK for Android app work

## Install

```powershell
cd C:\Users\naman\Desktop\wizlight-master
python -m pip install -e .
python -m pip install -r requirements.txt
```

For calibration tools:

```powershell
python -m pip install -e ".[calibration]"
```

For development:

```powershell
python -m pip install -e ".[dev,calibration]"
```

## Quick Start

Discover bulbs:

```powershell
wizlight discover
```

Launch the desktop GUI:

```powershell
python -m src.gui
```

Run tests:

```powershell
python -m ruff check .
python -m pytest
```

Build and test Android:

```powershell
cd wizlight-android
flutter pub get
flutter analyze
flutter test
flutter build apk --debug
```

## Desktop GUI

Start it with:

```powershell
python -m src.gui
```

The modern GUI is the primary desktop app. It includes:

- Bulbs: discover bulbs and remove stale/offline entries.
- Light Controls: toggle power, turn off, brightness, color picker, presets, and color temperature.
- Automation: screen sync and clap detection toggles.
- Screen Sync: monitor selection, responsiveness, smoothness, color punch, dark scene floor, GPU capture toggle, and a built-in guide explaining each control.
- Sync Debug: live capture method, FPS, send cadence, motion score, target/output color, and last error.
- Dashboard: per-bulb status view with automatic refresh.
- System tray: show app, toggle lights, screen sync, clap detection, brightness presets, color temperature presets, monitor selection, presets, and exit.

Removed from the GUI:

- Zone/region screen-sync controls. The backend still has zone-capable code, but the GUI now uses a simpler single ambient color path because that is the reliable workflow for one or more bulbs reacting to Chrome/YouTube/video content.
- Manual algorithm selector. The GUI now uses the tuned `auto` cinematic extractor instead of exposing weighted/k-means/histogram modes as user-facing knobs.
- Manual min-FPS and predictive toggles. Adaptive FPS and predictive smoothing remain enabled internally; the GUI exposes one practical Responsiveness slider instead.
- Separate tray Turn On and Turn Off entries. Tray now uses Toggle Lights plus direct brightness/temp presets.

## Windows Installer

Build a fresh desktop installer:

```powershell
python -m pip install -e ".[package]"
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

Build outputs:

```text
dist\WizLight.exe
dist\installer\WizLight-Setup.exe
```

`WizLight-Setup.exe` installs the app for the current Windows user under `%LOCALAPPDATA%\Programs\WizLight`, creates Start Menu and Desktop shortcuts, and launches the GUI after installation.

## Screen Sync

Screen sync captures the selected monitor, extracts a cinematic ambient color, maps it through bulb-aware color correction, and sends the latest color over UDP.

Recommended GUI settings:

- Responsiveness: `24-28 FPS` for YouTube and movies.
- Smoothness: `0.12-0.25` for responsive but stable color changes.
- Color Punch: `1.10-1.25x` for richer colors without overcooking whites.
- Dark Scene Floor: `10-25` for accurate dark scenes; raise it if the bulb gets too dim.
- Fast GPU capture: on. The app falls back automatically when DXCam cannot capture the selected monitor.
- Monitor: select the display where Chrome/YouTube is playing.

Script mode:

```powershell
python scripts/sync_screen.py
```

## Clap Detection

Clap detection listens to the default microphone and toggles configured bulbs. It uses peak/RMS checks, adaptive noise floor tracking, cooldown, and double-clap mode to reduce false triggers.

Run from GUI, or use:

```powershell
python scripts/clap_toggle.py
python scripts/test_clap.py
python scripts/list_mics.py
python scripts/debug_mic.py
```

## CLI

```powershell
wizlight discover
wizlight status
wizlight on
wizlight off
wizlight toggle
wizlight brightness 200
wizlight color 255 100 50
wizlight temp 4000
wizlight preset movie
wizlight preset
wizlight serve
```

Most commands accept `-i IP` to target one bulb instead of all configured bulbs.

`wizlight serve` starts the WebSocket server for external clients. The Android app in this repo does not require it for normal direct-to-bulb sync.

## Android App

The Flutter app lives in `wizlight-android/`.

Current Android features:

- Discover WiZ bulbs with UDP broadcast.
- Add bulb IP manually.
- Save bulbs and sync settings locally.
- Request Android screen-capture permission.
- Run native Android screen capture in a foreground service.
- Map screen colors to WiZ bulb payloads.
- Send direct UDP `setPilot` packets to bulbs.
- Show runtime FPS, send rate, updates, and output color.

Android commands:

```powershell
cd wizlight-android
flutter pub get
flutter analyze
flutter test
flutter run
flutter build apk --debug
```

Debug APK output:

```text
wizlight-android/build/app/outputs/flutter-apk/app-debug.apk
```

## Calibration And Hardware Accuracy

Camera calibration:

```powershell
python scripts/calibrate_bulb_camera.py --ip 192.168.1.3 --camera 0
```

Video color verification:

```powershell
python scripts/verify_video_color_mapping.py C:\path\to\video.mp4 --ip 192.168.1.3 --camera 0 --roi 120,80,90,90
```

Hardware accuracy runner:

```powershell
Copy-Item hardware_targets.example.json hardware_targets.json
notepad hardware_targets.json
python scripts/run_hardware_color_accuracy.py --manifest hardware_targets.json
python scripts/run_hardware_color_accuracy.py --manifest hardware_targets.json --execute
```

Pytest hardware gate:

```powershell
$env:RUN_HARDWARE_TESTS = "1"
python -m pytest -m hardware
```

Normal tests skip real bulb/camera access unless `RUN_HARDWARE_TESTS=1`.

## File Structure

```text
.
├── src/
│   ├── cli/
│   │   └── commands.py                 # wizlight CLI
│   ├── core/
│   │   ├── async_runtime.py            # background asyncio loop helpers
│   │   ├── bulb_controller.py          # pywizlight wrapper and payload mapping
│   │   ├── calibration.py              # calibration/tone LUT persistence
│   │   ├── color_mapping.py            # WiZ bulb gamut and white-channel mapping
│   │   └── config.py                   # ~/.wizlight/config.json schema
│   ├── features/
│   │   ├── alexa_bridge.py             # fauxmo bridge helpers
│   │   ├── clap_detector.py            # adaptive clap detection
│   │   ├── fast_udp_sender.py          # low-latency UDP sender pool
│   │   ├── screen_sync.py              # base capture/color extraction helpers
│   │   ├── screen_sync_v2.py           # optimized DXCam/MSS screen sync
│   │   ├── sync_dispatcher.py          # coalesced async fallback sender
│   │   └── websocket_server.py         # optional external WebSocket server
│   └── gui/
│       ├── __main__.py                 # python -m src.gui entrypoint
│       ├── modern_app.py               # primary desktop GUI
│       ├── ctk_compat.py               # CustomTkinter compatibility patch
│       └── components/
│           ├── color_wheel.py
│           ├── dashboard.py
│           ├── smooth_scrollable_frame.py
│           └── tray.py
├── scripts/
│   ├── calibrate_bulb_camera.py
│   ├── clap_toggle.py
│   ├── color_preset.py
│   ├── diagnose_bulb.py
│   ├── list_mics.py
│   ├── network_diagnostic.py
│   ├── run_hardware_color_accuracy.py
│   ├── sync_screen.py
│   ├── test_clap.py
│   └── verify_video_color_mapping.py
├── tests/                              # Python unit and hardware-gated tests
├── docs/
│   ├── alexa-setup.md
│   └── hardware-color-accuracy.md
├── wizlight-android/                   # Flutter + native Android screen sync app
├── hardware_targets.example.json       # hardware test template
├── pyproject.toml
└── requirements.txt
```

## Configuration

Desktop config is stored at:

```text
%USERPROFILE%\.wizlight\config.json
```

Important fields:

```json
{
  "bulbs": [
    {"ip": "192.168.1.100", "name": "Bulb 1", "mac": "aabbccddeeff"}
  ],
  "screen_sync": {
    "enabled": false,
    "mode": "single",
    "monitor": 1,
    "max_fps": 24,
    "smoothing": 0.2,
    "color_boost": 1.18,
    "min_brightness": 20,
    "ignore_letterbox": true,
    "use_gpu": true
  },
  "clap": {
    "enabled": false,
    "double_clap": true
  }
}
```

## Network Notes

- Keep bulbs and controller on the same LAN/VLAN.
- Allow UDP `38899` and `38900` locally.
- Do not expose WiZ local control ports to the internet.
- If discovery fails, try disabling guest-network isolation or adding a firewall rule for Python/Flutter.
- In the WiZ app, make sure local communication is enabled.

## Troubleshooting

No bulbs discovered:

- Confirm the bulb works in the WiZ app.
- Confirm desktop/Android and bulb are on the same network.
- Check Windows Firewall for Python.
- Check router AP isolation/guest network settings.

Wrong monitor in screen sync:

- Use the GUI Screen Sync monitor dropdown.
- Or use the tray Monitor menu when multiple displays are connected.
- Restart screen sync after changing monitors if it was already running.

Laggy screen sync:

- Use Fast GPU capture.
- Keep Responsiveness around 24-28 FPS.
- Lower Color Punch if the bulb appears to stutter on saturated scenes.
- Close other heavy screen capture apps.

Washed-out colors:

- Recalibrate with `scripts/calibrate_bulb_camera.py`.
- Keep Color Punch moderate.
- The code explicitly sends `c: 0` and `w: 0`/mapped white values where needed to avoid firmware-inferred white-channel washout.

Clap detection false triggers:

- Use double-clap mode.
- Move the mic away from speakers.
- Increase cooldown or thresholds in `%USERPROFILE%\.wizlight\config.json`.

## Verification

Recommended before committing:

```powershell
python -m ruff check .
python -m pytest
cd wizlight-android
flutter analyze
flutter test
flutter build apk --debug
```

## License

MIT. See [LICENSE](LICENSE).
