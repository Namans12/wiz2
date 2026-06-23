# CHECKS.md — wizlight-build
# Run every check before reporting a build/release as done.
# If any check fails, fix the issue and re-run all checks from the top.

## Pre-flight checklist

### Structure checks
- [ ] The intended artifact exists on disk at its expected path:
      - Android: `wizlight-android\build\app\outputs\flutter-apk\app-release.apk`
      - Windows desktop: `dist\WizLight.exe` (+ `dist\installer\WizLight-Setup.exe`)
      - macOS desktop: `dist/WizLight.app` (+ optional `dist/WizLight-macOS.dmg`) — only if built on macOS
- [ ] Artifact size is plausible (APK ≈ 40–60 MB; not 0 bytes, not a few KB)
- [ ] Artifact LastWriteTime is from this session (not a stale prior build)
- [ ] No stray temp files or half-written edits left behind

### Code / parity checks
- [ ] Every edited `.py` parses: `python -c "import ast; ast.parse(open('<file>',encoding='utf-8').read())"`
- [ ] `flutter doctor` shows no blocking issues (Android toolchain OK)
- [ ] If color logic changed: Android Kotlin and Python `src/core/color_mapping.py` still agree on CCT split, saturation gate, and profile biases
- [ ] No placeholder text (TODO/FIXME) left in code paths that shipped
- [ ] Any new Python dependency is in `requirements.txt` with the correct platform marker

### Behavior checks (device / runtime)
- [ ] For Android color changes: note what the Diagnostics panel should show (deviceProfile, detectedDisplayGamut, primaryHdrType, captureBufferFormat) so the user can confirm on-device
- [ ] sRGB laptop baseline is not regressed by a change meant only for a wide-gamut profile

### State checks
- [ ] `state.json` updated with this session's artifact and history entry
- [ ] `checkpoint.log` shows all steps as OK

### Final gate
Report success only after every box above is checked.
If any check fails, fix it and re-run this entire checklist from the top.
Never report a partial or unverified build as done. Never claim a macOS artifact
was produced on a Windows host.
