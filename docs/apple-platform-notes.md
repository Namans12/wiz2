# macOS And iOS Platform Notes

Apple support is a second milestone after Windows desktop and Android pass the production validation gate.

## Current State

- The desktop GUI is Python/CustomTkinter and optimized for Windows capture through DXCam with MSS fallback.
- The Android app is Flutter with native Android MediaProjection capture.
- There are no macOS or iOS app targets in the repo today.

## macOS Direction

- Keep the same Flutter UI model used by Android if a cross-platform desktop app becomes the long-term direction.
- Use macOS ScreenCaptureKit or CGDisplayStream for low-latency capture.
- Use the same runtime stats shape as desktop/Android: `requestedFps`, `currentFps`, `frameTimeMs`, `sendRateHz`, `motionScore`, `smoothingFactor`, `effectiveColorBoost`, `minBrightness`, `deltaThreshold`, `captureBackend`, `targetColors`, `outputColors`, `lastError`.
- Keep WiZ control local over UDP on the trusted LAN.

## iOS Direction

- Treat iOS as constrained until ReplayKit feasibility is validated for the exact screen-sync workflow.
- If full-device screen capture is not acceptable for App Store distribution, scope iOS to bulb control, discovery, diagnostics, and possibly media/app-specific sync.
- Reuse the Android Flutter data model and diagnostics JSON format where possible.

## Acceptance Before Implementation

Do not start Apple implementation until:

- Windows and Android pass automated gates.
- Visible controls have a completed truth audit.
- Diagnostics export exists on both active platforms.
- Hardware color validation thresholds are documented and repeatable.
