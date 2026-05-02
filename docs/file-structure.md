# File Structure

## Root

```text
.
├── src/                         # Desktop Python app package
├── scripts/                     # Diagnostics, calibration, and standalone tools
├── tests/                       # Python tests and hardware-gated tests
├── docs/                        # Project documentation
├── packaging/                   # Windows EXE and installer build scripts
├── skills/wiz_config/           # Local WiZ docs/config helper scripts
├── wizlight-android/            # Flutter + native Android sync app
├── hardware_targets.example.json
├── pyproject.toml
├── requirements.txt
├── README.md
└── LICENSE
```

## Desktop Package

```text
src/
├── cli/
│   └── commands.py              # `wizlight` CLI
├── core/
│   ├── async_runtime.py         # Background asyncio runner
│   ├── bulb_controller.py       # pywizlight wrapper and bulb operations
│   ├── calibration.py           # Calibration data and LUT persistence
│   ├── color_mapping.py         # WiZ gamut, white-channel, and RGB mapping
│   └── config.py                # Desktop config schema and persistence
├── features/
│   ├── alexa_bridge.py          # fauxmo Alexa bridge
│   ├── clap_detector.py         # Adaptive microphone clap detection
│   ├── fast_udp_sender.py       # Low-latency latest-wins UDP sender
│   ├── screen_sync.py           # Base screen-sync helpers
│   ├── screen_sync_v2.py        # Optimized DXCam/MSS screen sync
│   ├── sync_dispatcher.py       # Coalesced async fallback dispatcher
│   └── websocket_server.py      # Optional external WebSocket bridge
└── gui/
    ├── __main__.py              # `python -m src.gui`
    ├── ctk_compat.py            # CustomTkinter compatibility patch
    ├── modern_app.py            # Main desktop GUI
    └── components/
        ├── animations.py
        ├── color_wheel.py
        ├── dashboard.py
        ├── smooth_scrollable_frame.py
        └── tray.py
```

## Android App

```text
wizlight-android/
├── lib/
│   ├── main.dart
│   ├── models/
│   │   └── wiz_bulb.dart
│   ├── screens/
│   │   └── home_screen.dart
│   └── services/
│       ├── screen_capture_service.dart
│       ├── settings_service.dart
│       ├── sync_service.dart
│       └── wiz_bulb_service.dart
├── android/
│   └── app/src/main/
│       ├── AndroidManifest.xml
│       └── kotlin/com/wizlight/app/
│           ├── MainActivity.kt
│           └── ScreenCaptureService.kt
├── test/
│   └── tablet_sync_test.dart
├── pubspec.yaml
└── README.md
```

## Packaging

```text
packaging/
├── WizLight.spec
├── build_windows.ps1
├── make_icon.py
├── make_iexpress_installer.py
├── wizlight_gui_entry.py
├── assets/
│   └── wizlight.ico
└── installer/
    ├── install.cmd
    ├── install.ps1
    └── uninstall.cmd
```

## Safe Generated Files

These can be deleted and regenerated:

```text
.pytest_cache/
.ruff_cache/
build/
dist/
wizlight.egg-info/
**/__pycache__/
wizlight-android/.dart_tool/
wizlight-android/build/
wizlight-android/android/.gradle/
```

Root-level duplicate reference files can also be deleted if present:

```text
bulb_controller.py
clap_detector.py
color_mapping.py
fast_udp_sender.py
screen_sync_v2.py
```
