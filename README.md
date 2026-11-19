# WizLight

Local-network controller for Philips WiZ bulbs, for Windows desktop and Android (including an Android TV launcher entry). It focuses on local control, low-latency screen sync, clap detection, bulb calibration, and a clean GUI for daily use. A macOS packaging script is included but is not yet a supported target.

WiZ bulbs are controlled locally over UDP. Your bulbs and controlling device must be on the same trusted LAN, and local communication must be enabled in the WiZ app.

## Download

Release builds (Windows installer and Android APK) are coming. Until then, build from source using the instructions below.

## Documentation

- [Command Reference](docs/commands.md): setup, CLI, GUI, tests, Android builds, Windows installer, and cleanup commands.
- [File Structure](docs/file-structure.md): important files, generated files, and safe cleanup targets.
- [Control Truth Audit](docs/control-audit.md): visible controls, runtime metrics, and keep/hide/remove decisions.
- [Production Validation](docs/production-validation.md): automated, manual, packaging, and hardware gates.
- [Latency And Accuracy Testing](docs/latency-and-accuracy-testing.md): synthetic benchmarks, fake UDP bulb, control-effect checks, and real hardware measurement.
- [Hardware Color Accuracy](docs/hardware-color-accuracy.md): real-bulb camera verification flow.
- [macOS Platform Notes](docs/apple-platform-notes.md): platform limitations and earlier Apple research.
- [Android App README](wizlight-android/README.md): Android screen-sync usage and build notes.

## Highlights

- Desktop GUI with discovery, power, brightness, RGB color picker, color temperature, presets, screen sync, clap detection, dashboard, and tray controls.
- Screen sync tuned for YouTube, Chrome, movies, and games. Capture uses DXCam with a WinRT backend (no yellow capture border), with MSS as a fallback.
- Low-latency link: a latest-wins UDP sender capped to a WiZ-friendly rate, plus an optional keep-awake query chain that holds each bulb's Wi-Fi radio awake and confirms delivery. On one measured WiZ bulb, updates arrived in about 18 ms instead of about 117 ms. The cost is roughly 110 small packets per second per bulb while syncing; turn it off with `keep_awake` in the config.
- Bulb-aware color mapping with explicit white-channel handling for WiZ full-color bulbs, plus an optional physical colour model (`mixing_model: "physical"`) that solves the additive mix of the bulb's five LED channels. It is experimental and its spectral assumptions are not yet verified with a colorimeter. An optional orange trim (`physical_color_trim`) applies to it.
- Clap detection with adaptive room-noise tracking, transient filtering, and double-clap mode.
- Camera calibration and opt-in hardware color accuracy tests.
- Android Flutter app with direct WiZ discovery, native MediaProjection capture, and direct UDP screen sync, plus an Android TV launcher entry and banner.
- Optional CLI automation.

## Requirements

- Python 3.10+
- Windows recommended for the modern desktop GUI and DXCam capture
- WiZ bulbs on the same LAN as the desktop or Android device
- WiZ app setup completed with local communication enabled
- UDP `38899` and `38900` allowed on the local network
- Optional: microphone permission for clap detection
- Optional: OpenCV-compatible camera for real-bulb calibration
- Optional: Flutter SDK and Android SDK for Android app builds

## Quick Start

Run these from the repository folder (`<repo folder>`).

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e ".[dev,calibration,package]"
python -m src.gui
```

Discover bulbs from the CLI:

```powershell
wizlight discover
```

Run validation:

```powershell
python -m ruff check .
python -m pytest
```

Full command list: [docs/commands.md](docs/commands.md).

## Desktop GUI

Start:

```powershell
python -m src.gui
```

The GUI includes:

- Bulb discovery and stale/offline bulb removal.
- Power, brightness, color, color temperature, and presets.
- Screen sync with monitor selection, responsiveness, smoothing, color punch, dark-scene floor, GPU capture toggle, and built-in control explanations.
- Sync debug panel showing capture method, FPS, send cadence, motion score, target color, output color, and last error.
- Zone layout with saved bulb assignments for screen sync.
- Clap detection toggle.
- Dashboard with per-bulb state.
- Tray controls for lights, screen sync, clap detection, brightness, color temperature, monitor selection, presets, and exit.

Removed from the desktop GUI:

- Manual algorithm selector. The GUI uses the tuned `auto` cinematic extractor.
- Manual min-FPS and predictive toggles. Adaptive FPS and predictive smoothing remain internal.
- Separate tray Turn On and Turn Off entries. Tray uses Toggle Lights plus direct brightness and temperature presets.

## Command Line

Installing the package provides the `wizlight` command:

```powershell
wizlight --help
wizlight discover
wizlight list-bulbs
wizlight status
wizlight on
wizlight off
wizlight toggle
wizlight brightness 80
wizlight color 255 80 20
wizlight temp 4000
wizlight preset warm
```

Bulb-control commands accept `-i IP` to target one bulb. Other commands: `add-bulb`, `remove-bulb`, `prune-bulbs`, `config-path`. The standalone screen-sync script is run with `python scripts\sync_screen.py`. See [docs/commands.md](docs/commands.md) for the full list.

## Screen Sync

Desktop screen sync captures the selected monitor, extracts a cinematic ambient color, maps it through WiZ-aware color correction, and sends the latest color over UDP.

Current performance path:

- DXCam capture with the WinRT backend, which does not draw the yellow capture border, and MSS as a fallback.
- Stride-based hot-path sampling instead of PIL resizing.
- Adaptive FPS and adaptive send delta for static vs moving scenes.
- Explicit `c` and `w` fields in WiZ packets to avoid firmware-inferred white-channel washout.
- Prewarmed UDP sender sockets and byte-template packet encoding.
- Optional keep-awake link described under Highlights.

Recommended settings:

- Responsiveness: `24-28 FPS` for YouTube and movies.
- Smoothness: `0.12-0.25` for responsive but stable color changes.
- Color Punch: `1.10-1.25x`.
- Dark Scene Floor: `10-25`.
- Fast GPU capture: enabled.
- Monitor: select the display where Chrome/YouTube is playing.

UDP diagnostics count successful socket writes, not acknowledgements from a bulb. Synthetic benchmarks measure software processing only and exclude capture, network, and bulb latency.

## Android App

The Android app lives in `wizlight-android/`.

Current Android features:

- WiZ discovery over local UDP broadcast.
- Manual add-by-IP fallback.
- Local bulb/settings persistence.
- Native Android MediaProjection screen capture in a foreground service.
- Single ambient mode and two-bulb left/right zone mode.
- Cinematic color extraction, palette hold, smoothing, adaptive FPS, adaptive send delta, letterbox rejection, and WiZ-aware color mapping.
- Direct UDP `setPilot` packets with explicit white-channel fields.
- Runtime stats for FPS, send rate, motion score, extractor, target color, output color, and errors.
- Android TV launcher entry and banner. Remote-control navigation and physical-device behavior still need testing.

Settings changed during playback require **Apply / restart**. Screen capture supports the configured zones and crop/HDR path. USB playback currently samples one full-frame SDR image and does not implement those options. Protected content and external HDMI capture remain platform limitations.

Build (run from the repository folder):

```powershell
cd wizlight-android
flutter pub get
flutter analyze
flutter test
flutter build apk --debug
flutter build apk --release
```

Use the debug build for development and the release build for installing on a device. More Android commands: [docs/commands.md](docs/commands.md#android).

## Windows Installer

Build:

```powershell
.\packaging\build_windows.ps1
```

Outputs:

```text
dist\WizLight\WizLight.exe   (onedir folder; keep it together)
dist\WizLight-portable.zip   (unzip anywhere and run WizLight.exe)
dist\installer\WizLight-Setup.exe
```

Installer details and commands: [docs/commands.md](docs/commands.md#windows-installer).

## Calibration And Hardware Accuracy

Camera calibration and real-bulb validation are available through scripts and hardware-gated tests. Normal tests do not control real bulbs unless explicitly enabled.

Start with:

```powershell
python scripts\calibrate_bulb_camera.py --ip 192.168.1.3 --camera 0
```

Full flow: [docs/hardware-color-accuracy.md](docs/hardware-color-accuracy.md).

## Configuration

Desktop config:

```text
%USERPROFILE%\.wizlight\config.json
```

Typical screen-sync settings:

```json
{
  "screen_sync": {
    "enabled": false,
    "mode": "single",
    "monitor": 1,
    "max_fps": 24,
    "smoothing": 0.2,
    "color_boost": 1.18,
    "min_brightness": 20,
    "ignore_letterbox": true,
    "use_gpu": true,
    "mixing_model": "heuristic",
    "keep_awake": true
  }
}
```

Desktop configuration saves are atomic, and corrupt desktop and Android settings are preserved for recovery.

## Network Notes

- Keep bulbs and controller on the same LAN/VLAN.
- Enable local communication in the WiZ app.
- Allow UDP `38899` and `38900` locally.
- Do not expose WiZ local-control ports to the internet.
- If discovery fails, check guest-network isolation, AP isolation, and firewall rules.

## Verification

Recommended before packaging or committing:

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
