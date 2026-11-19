# Control Truth Audit

This audit lists user-facing controls that should remain visible because they map to real runtime behavior. Controls with subtle or scene-dependent impact stay in Advanced/Diagnostics instead of the main cockpit.

## Desktop GUI

| Control | Config key | Runtime field / metric | Expected effect | Decision |
| --- | --- | --- | --- | --- |
| Start / Stop Screen Sync | `screen_sync.enabled` | `running`, `updatesSent` | Starts or stops capture and UDP sending. | Keep visible |
| Monitor | `screen_sync.monitor` | `captureBackend`, target/output color changes | Selects the display captured by DXCam/MSS. | Keep visible |
| Fast GPU capture | `screen_sync.use_gpu` | `captureBackend` | Uses DXCam when available, with MSS fallback. | Keep visible |
| Low-latency link | `screen_sync.keep_awake` | Advanced Debug cadence line (delay, awake %, delivered, signal) | Holds each bulb's radio awake with read-only status queries and confirms delivery from the replies. | Keep visible |
| Responsiveness | `screen_sync.max_fps` | `requestedFps`, `currentFps`, `frameTimeMs` | Raises the target capture cadence, capped by bulb/network limits. | Keep visible |
| Smoothness | `screen_sync.smoothing` | `smoothingFactor` | Lower values react faster; higher values reduce flicker. | Keep visible |
| Color Punch | `screen_sync.color_boost` | `effectiveColorBoost`, `outputColors` | Increases saturation after scene-aware attenuation. | Keep visible |
| Dark Scene Floor | `screen_sync.min_brightness` | `minBrightness`, `outputColors` | Prevents very dark frames from becoming invisible on the bulb. | Keep visible |
| Ignore letterbox | `screen_sync.ignore_letterbox` | target/output color during letterboxed video | Always enabled in desktop GUI for video-first sync. | Hide from main UI |
| Adaptive FPS | `screen_sync.adaptive_fps` | `currentFps`, `motionScore` | Adjusts FPS by motion. | Internal |
| Predictive smoothing | `screen_sync.predictive_smoothing` | `prediction_weight` | Reduces perceived lag during moderate motion. | Internal |
| Zone layout | `screen_sync.bulb_layout` | `mode`, `targetColors` | Routes screen regions to bulbs. | Removed from desktop GUI until multi-zone UX is revalidated |

## Android App

| Control | Native argument | Runtime field / metric | Expected effect | Decision |
| --- | --- | --- | --- | --- |
| Start / Stop Screen Sync | `startCapture` / `stopCapture` | `updatesSent`, foreground service state | Starts or stops MediaProjection capture and UDP sending. | Keep visible |
| Single / Left + Right | `mode` | `mode`, `targetColors`, `outputColors` | Single ambient or two-bulb left/right sync. | Keep visible |
| Max FPS | `maxFps` | `requestedFps`, `currentFps`, `frameTimeMs` | Raises target capture cadence. | Keep visible |
| Smoothing | `smoothing` | `smoothingFactor` | Controls output transition speed. | Keep visible |
| Color Boost | `colorBoost` | `effectiveColorBoost`, `outputColors` | Increases saturation before WiZ mapping. | Keep visible |
| Ignore letterbox bars | `ignoreLetterbox` | target/output color during letterboxed video | Crops black bars from video sampling. | Keep visible |
| Min FPS, sample size, edge weight, min delta, predictive smoothing | persisted settings only | diagnostics stats | Real but advanced tuning values. | Hide from main UI |

## Acceptance Rule

A visible control must meet all three conditions:

1. It changes a persisted setting or native service argument.
2. It changes a runtime debug/diagnostics metric.
3. It has a user-observable effect in the manual UX checklist.
