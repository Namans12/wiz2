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
- After the tablet feed was resumed, a repeat using the same 55-second pattern
  observed 18 screen changes and 16 matching wall changes. The median was
  312.5 ms and the slowest matched change was 500 ms at the same 0.15
  threshold. The camera interval median was 31 ms (maximum 78 ms). Windows
  Graphics Capture processed 837 frames and reported no capture error. Two
  unmatched camera transitions do not by themselves establish whether the
  bulb, network, or image measurement missed the change. The original lamp
  settings were restored with a successful device response.
- A separate exploratory run disabled adaptive FPS and prediction, set
  smoothing to zero, requested 30 FPS, and reduced the color delta threshold
  to 1. It processed 1,553 frames in 55 seconds versus 836 in the earlier
  run, but the observed median was 313 ms with 14 of 18 cuts matched (earlier:
  312.5 ms with 18 of 18). These small, different samples do not establish a
  latency improvement. The user's settings were not changed.
- A later 25-second animated-content run reported mean capture time 73.4 ms,
  p95 205.7 ms, and mean processing time 7.6 ms (p95 11.4 ms), based on the
  engine's rolling samples. These are application timings on this setup,
  not screen-to-lamp measurements. A small isolated WinRT capture experiment
  using copied versus reusable frames did not establish that removing the
  buffer copy improved capture time, so that optimization was not adopted.

## Color observations

The desktop UDP encoder previously rounded channels to steps of eight before
sending. A direct device test with exact RGBW values `(123, 5, 0, 17)` was
accepted and `getPilot` reported those same values; the previous lamp state
was then restored successfully. Channel rounding has been removed. This
confirms payload preservation, not measured emitted color accuracy. A local
20,000-payload random-input timing experiment before the change measured
about 5.6 microseconds per encoding with rounding and 5.2 microseconds with
rounding disabled on this machine; this is a microbenchmark, not an
end-to-end latency comparison or a measurement of Wi-Fi traffic.

In a later capture investigation, GDI/MSS returned black on both displays
while Windows Graphics Capture returned the changing animation seen on the
HDMI monitor through the camera. A source-engine probe recovered to Windows
Graphics Capture and emitted changing color targets without capture errors.
A 25-second GUI run with the lamp configured also reported Windows Graphics
Capture, 189 processed frames, 98 submitted color updates and no capture
error; the previous lamp state was restored with a successful response.
Submitted updates do not prove UDP delivery or optical color accuracy, and
this animation run did not produce a valid camera latency measurement.
When the CPU reference is black, recovery cannot independently verify the
candidate against that reference; selection still relies on the configured
GPU output index corresponding to the intended monitor.

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
