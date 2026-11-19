# WizLight Android Screen Sync

Android companion app for syncing tablet/phone screen colors directly to WiZ bulbs over the local network. It does not require the Windows app for normal direct-to-bulb sync.

## Features

- Local WiZ discovery over UDP broadcast.
- Manual add-by-IP fallback.
- Saved bulbs and sync settings through `shared_preferences`.
- Native Android MediaProjection capture through `ScreenCaptureService`.
- Foreground service while capture is active.
- Single ambient mode for one or more bulbs.
- Left/right zone mode for two-bulb ambient setups.
- Cinematic extraction, palette hold, smoothing, adaptive FPS, adaptive send delta, and letterbox rejection.
- WiZ-aware RGB plus explicit `c`/`w` payloads to avoid washed-out firmware white-channel inference.
- Runtime status for mode, FPS, send rate, motion score, smoothing, extractor, target color, output color, update count, and errors.
- In-app guide explaining sync controls.

## Requirements

- Android 7.0+ API 24+
- WiZ bulbs on the same Wi-Fi/LAN as the Android device
- Local communication enabled in the WiZ app
- Android screen-capture permission granted when starting sync

## Build

```powershell
cd <repo folder>\wizlight-android
flutter pub get
flutter analyze
flutter test
flutter build apk --debug
```

Debug APK:

```text
build\app\outputs\flutter-apk\app-debug.apk
```

Release APK:

```powershell
flutter build apk --release
```

App bundle:

```powershell
flutter build appbundle --release
```

## Install

```powershell
adb install .\build\app\outputs\flutter-apk\app-debug.apk
```

## Usage

1. Open the app.
2. Tap the search icon to discover WiZ bulbs.
3. If discovery misses a bulb, tap the add icon and enter its IP address.
4. Assign bulbs to `Left` or `Right` if using two-bulb zone mode.
5. Pick `Single` or `Left + Right`.
6. Adjust FPS, smoothing, color boost, and letterbox handling.
7. Tap `Start Sync` and grant Android screen-capture permission.

## Tuning

- `Single` is best for one bulb or multiple bulbs showing the same ambient color.
- `Left + Right` is for two-bulb setups placed around the display.
- Higher `Max FPS` reacts faster; WiZ bulbs generally cap around 28 updates per second.
- Lower `Smoothing` feels more immediate; higher values reduce flicker.
- Higher `Color Boost` increases saturation; too much can make scenes look artificial.
- Keep `Ignore letterbox bars` enabled for movies and YouTube content with black bars.

## Architecture

```text
lib/
├── main.dart
├── models/
│   └── wiz_bulb.dart
├── screens/
│   └── home_screen.dart
└── services/
    ├── screen_capture_service.dart
    ├── settings_service.dart
    ├── sync_service.dart
    └── wiz_bulb_service.dart

android/app/src/main/kotlin/com/wizlight/app/
├── MainActivity.kt
└── ScreenCaptureService.kt
```

## Troubleshooting

No bulbs discovered:

- Confirm local communication is enabled in the WiZ app.
- Keep the Android device and bulbs on the same LAN.
- Disable guest-network or AP isolation.
- Use manual Add IP if UDP broadcast is blocked.

Screen capture starts but lights do not react:

- Confirm bulbs are configured in the app.
- Confirm the bulbs respond in the WiZ app.
- Some DRM-heavy apps can return black frames through MediaProjection.

Build fails on Windows:

```powershell
cd <repo folder>\wizlight-android\android
.\gradlew.bat --stop
cd ..
flutter clean
flutter pub get
```
