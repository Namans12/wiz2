# HDMI capture and light measurements — 2026-11-11

## Scope

Windows laptop with a 1920×1080 HDMI display, one WiZ RGBW bulb, and an
Android tablet used as a Windows virtual camera. The screen and the light's
reflection were visible in the same camera view. Camera exposure, white
balance, screen color profile and wall reflectance were not calibrated.

## Capture and timing

- DXGI capture returned black while CPU capture saw a picture. Windows
  Graphics Capture followed full-screen red and blue test patterns. The app
  switched from DXGI to Windows Graphics Capture in a live test.
- The test window was moved and maximized with per-monitor DPI awareness.
  Earlier measurements with a partly covered screen should not be treated as
  full-screen results.
- A 55-second red/blue run yielded 18 matched screen and wall transitions.
  At a normalized red/blue threshold of 0.15, median delay was 312.5 ms and
  the slowest matched delay was 563 ms. Median frame-arrival interval was
  31 ms, and the largest gap was 94 ms.
- Using the same samples, thresholds of 0.05 and 0.25 gave medians of
  265.5 ms and 374.5 ms. These are camera threshold-crossing estimates,
  not calibrated display-to-photon latency. Virtual-camera buffering,
  rolling shutter, auto exposure, and screen spill on the wall can bias them.

## Color observations

Seven static scenes (red, green, blue, cyan, magenta, yellow and white)
produced the corresponding application color targets through Windows Graphics
Capture. This verifies the basic scene path, not absolute color accuracy.

The bulb reported one white channel. For a white input, its current heuristic
mapper produced `(0, 0, 0, 255)`, using only that channel. A camera comparison
showed a warm-looking reflection for white-channel-only output and a
violet-looking reflection for RGB-only white. Mixed RGB and white outputs
changed the observed balance. Camera auto processing and wall reflectance make
these observations insufficient to select a calibrated correction or a new
default. Repeat with fixed camera settings and a measured reference before
claiming color accuracy or modifying the default white mix.

In a follow-up exploratory sweep with RGB drive fixed at 180, white drive at
96, and green drive varied, the camera's wall-to-screen relative RGB ratios
changed from approximately `(1.073, 0.833, 1.094)` at green 180 to
`(1.029, 0.925, 1.047)` at green 255. Repeating the first setting gave
`(1.081, 0.833, 1.086)`. This suggests a possible device-specific correction,
but is not a calibration: the tablet virtual camera rejected requests to lock
both automatic white balance and manual exposure. No correction was applied.
