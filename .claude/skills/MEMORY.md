# MEMORY.md — wizlight-build
# Read this file at the start of every session to restore state.
# Write updated state back to state.json at the end of every session.

## State file location
`.claude\skills\state\state.json`

## How to read state at session start
1. Read `.claude\skills\state\state.json`.
2. If it doesn't exist, use the default state below and create it.
3. Parse the JSON and use it to inform the session (last artifact, pending follow-ups, device profiles touched).

## State schema
```json
{
  "app_name": "wizlight-master",
  "version": "0.1.0",
  "last_run": null,
  "session_count": 0,
  "user_preferences": {
    "primary_target_device": "Sony Bravia 8 Mark II (2025, QD-OLED, Google TV)",
    "build_targets": ["android-apk", "windows-exe", "macos-app"],
    "color_parity": "Android Kotlin and Python color_mapping.py must stay in sync"
  },
  "last_output": null,
  "history": [],
  "flags": {
    "setup_complete": true,
    "last_error": null,
    "release_signing_configured": false
  }
}
```

## How to update state at session end
- Set `last_run` to today's date.
- Increment `session_count` by 1.
- Set `last_output` to the artifact produced (e.g. `wizlight-android/build/app/outputs/flutter-apk/app-release.apk`).
- Append a one-line entry to `history`: `{ "date": "...", "action": "...", "output": "..." }`.
- Set `flags.last_error` to `null` on success, or to the error message on failure.

## What to remember across sessions
- **Primary device:** Sony Bravia 8 Mark II — warm/orange glow was the original bug; fixed via gamut auto-detect + CCT cold/warm split + OLED true-black + per-HDR-type tone curves.
- **Device profiles:** `generic`, `mac`, `bravia` (cool bias +0.10), `wide_gamut_hdr` (+0.05). Saturation gate: 0.10 generic / 0.09 WCG / 0.08 Bravia.
- **APK is debug-signed** — no release keystore yet (`flags.release_signing_configured=false`). Fine for sideloading; needed for Play Store.
- **macOS build** must run on a macOS host (`bash packaging/build_macos.sh`); cannot be produced on the Windows dev machine.
- **Diagnostics panel** is the source of truth for what actually engaged on-device (`deviceProfile`, `detectedDisplayGamut`, `primaryHdrType`, `captureBufferFormat`).

## Privacy
Do not store raw file contents, IP addresses, or MAC addresses in state.json. Store only filenames, metadata, and preferences.
