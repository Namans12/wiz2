# WizLight (Dupe of [wizlight](https://github.com/Namans12/wizlight))

Working copy of **[`wizlight`](https://github.com/Namans12/wizlight)**, branched off on 2026-05-01. This is where the packaging, colour-science, and native-Android work happened.

> ### ⚠️ Read this before cloning
>
> **The default branch (`master`) is a single "first commit" snapshot from 2026-05-01 and contains none of the work below.** Nine branches sit unmerged on top of it, in a linear chain. If you clone this repo normally you get the May 1st snapshot, which is *older* than what is in `wizlight`.
>
> To get the latest state: `git checkout prefinal-claude-v2` — but note that branch is knowingly unfinished (see [Branches](#branches)).

## What this repo is

Same product as `wizlight`: a local-network controller for Philips WiZ bulbs with screen sync, clap detection, and Alexa support. For what the software *does*, how to install it, and the full CLI reference, see the **[canonical README in `wizlight`](https://github.com/Namans12/wizlight#readme)** — that documentation applies here too.

This README covers only what makes this copy different.

## How it differs from `wizlight`

Compared file-by-file against `wizlight@master` (90 files each; 66 identical, 15 modified, 9 added, 9 removed):

### Added here

| File | Purpose |
|---|---|
| `screen_sync.py`, `screen_sync_v2.py`, `clap_detector.py`, `color_mapping.py`, `fast_udp_sender.py` | Top-level copies of `src/` modules — **removed again on `branch1`** as duplicates |
| `BUGFIXES.md` | Running bug log — also removed on `branch1` |
| `src/features/fast_udp_sender.py` | Lower-latency UDP write path for sync |
| `src/features/sync_dispatcher.py` | Routes sync frames to the right sender |
| `src/gui/ctk_compat.py` | customtkinter compatibility shim across versions |

### Removed here

The entire **`chrome-extension/`** directory (9 files) — the *WizLight Video Sync* MV3 browser extension. **It exists only in `wizlight`.** If you need browser video sync, that repo is the only source.

### Modified here

Incremental growth across shared modules, largest first:

| File | `wizlight` | here | Δ |
|---|---|---|---|
| `tests/test_features.py` | 506 | 634 | +128 |
| `src/core/bulb_controller.py` | 558 | 647 | +89 |
| `src/gui/modern_app.py` | 991 | 1022 | +31 |
| `src/gui/app.py` | 883 | 912 | +29 |
| `src/features/screen_sync_v2.py` | 1125 | 1150 | +25 |
| `src/features/screen_sync.py` | 484 | 506 | +22 |
| `tests/test_async_runtime.py` | 44 | 66 | +22 |
| `tests/test_bulb_controller.py` | 323 | 340 | +17 |

Plus smaller edits to `scripts/sync_screen.py`, `src/cli/commands.py`, `src/core/color_mapping.py`, `src/core/async_runtime.py`, `src/features/clap_detector.py`, `src/features/websocket_server.py`, and `src/gui/components/dashboard.py`.

## Branches

Strictly linear — each branch contains everything before it. Pick the furthest point that meets your needs.

| Branch | Date | Ahead of `master` | What it adds |
|---|---|---|---|
| `master` *(default)* | 2026-05-01 | — | Snapshot of `wizlight` plus the duplicated top-level modules |
| `branch1` | 2026-05-01 | +1 | **Cleanup.** Deletes the duplicated top-level `.py` files and `BUGFIXES.md`; everything consolidates into `src/` |
| `branch2` | 2026-05-01 | +2 | **Windows packaging.** `build_windows.ps1`, `installer/{install,uninstall}.{cmd,ps1}`, IExpress installer generator, `make_icon.py`, app `.ico`. Adds hardware colour-accuracy harness (`scripts/run_hardware_color_accuracy.py` + tests + `docs/hardware-color-accuracy.md`). Retires the legacy `src/gui/app.py` |
| `branch3` | 2026-05-03 | +4 | **Flutter Android app.** Full `wizlight-android/lib/` tree — `main.dart`, `models/wiz_bulb.dart`, `screens/home_screen.dart`, and services for discovery, screen capture, settings, and sync. Adds `docs/commands.md`, `docs/file-structure.md`, `FUTURE_FEATURES.md` |
| `branch4` | 2026-05-05 | +5 | **Branding.** Windows icon plus Android launcher icons at all five mipmap densities |
| `branch5` | 2026-05-07 | +6 | **Diagnostics & validation.** `src/core/diagnostics.py`; `scripts/fake_wiz_bulb_udp.py` (a fake bulb that speaks the UDP protocol, so you can test without hardware); `benchmark_screen_sync.py`; `measure_control_effects.py`; docs on latency/accuracy testing, production validation, control audit, and Apple platform notes |
| `branch6` | 2026-05-07 | +7 | **Colour science.** `src/core/color_science.py` + `tests/test_color_science.py`; reworks `bulb_controller`, `color_mapping`, and `config` around it |
| `prefinal` | 2026-06-23 | +8 | **macOS packaging + native Android.** `packaging/build_macos.sh`; `src/core/display_detection.py` for multi-monitor handling; Kotlin-native path — `ColorSyncEngine.kt`, `PlayerActivity.kt`, `SyncBridge.kt` — running alongside the Flutter layer |
| `prefinal-claude-v2` | 2026-07-18 | +9 | **⚠️ Unfinished.** LUT pipeline: `scripts/export_lut_for_android.py` and a baked `wiz_rgbcw_lut_v1.bin` shipped into the Android assets; `scripts/probe_wiz_rate.py` for rate-limit probing; `packaging/frozen_diag.py`; `docs/device-display-matrix.md`. Commit message: *"some issues still not fixed"* |

### Which branch should you use?

- **Stable and complete** → `branch6` (2026-05-07). Last point where everything was in a finished state.
- **macOS or native Android** → `prefinal` (2026-06-23).
- **Latest work, expect breakage** → `prefinal-claude-v2`.
- **Chrome extension** → not here. Use [`wizlight`](https://github.com/Namans12/wizlight).

## Getting the branch you want

```bash
git clone https://github.com/Namans12/wiz2.git
cd wiz2
git checkout branch6
pip install -e .
```

## Recommendation

The two repos have genuinely diverged and neither contains everything:

- `wizlight` — has the Chrome extension, has a merged and coherent `master`
- `wiz2` — has Windows/macOS packaging, colour science, diagnostics, native Android — all stranded on unmerged branches

Consolidating is worth doing: merge `wiz2@branch6` (or `prefinal`) back into `wizlight`, keep the Chrome extension, and retire this repo. Until then, treat `wizlight` as the reference for *what the software does* and this repo as the source for packaging and colour-science work.

## License

[MIT](LICENSE) — same as the upstream repo.
