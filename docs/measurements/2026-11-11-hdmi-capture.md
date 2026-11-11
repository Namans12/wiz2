# HDMI capture and light measurements — 2026-11-11

## Scope

Windows laptop with a 1920×1080 HDMI display, one WiZ RGBW bulb, and an
Android tablet used as a Windows virtual camera. The screen and the light's
reflection were visible in the same camera view. Camera exposure, white
balance, screen color profile and wall reflectance were not calibrated.

## Capture and timing

- A later tablet Pro Video recording showed 44 red/blue screen transitions.
  Using the video frame timestamps and the wall region's 0.15 red/blue
  threshold, 42 changes matched before the next screen change. The median
  matched delay was 333 ms, p95 1,833 ms, and maximum 1,933 ms. Four matched
  changes took 1.8–1.9 seconds. One blue change did not visibly arrive before
  the next red change, which also left that red change unmatched by the
  alternating-state detector. The earlier 1.5-second analysis cutoff hid
  those four late matches as unmatched. The recording was about 30 fps, so
  frame sampling alone resolves time only to roughly 33 ms. White balance and
  ISO were set manually; shutter/exposure mode was not confirmed locked.
  Screen light on the wall, exposure, display scanout and rolling shutter can
  bias threshold-based measurements. These results do not measure UDP delivery
  or prove the cause of the late/missed transition.
- A separate status-polling probe recorded app packet submissions and
  `getPilot` responses during red/blue transitions. In several slow cases,
  three local sends succeeded within roughly 100 ms but the bulb continued
  reporting its old color until after the 1.5-second refresh. This is
  consistent with burst loss, though successful local sends do not prove
  network delivery and polling can itself affect timing. A follow-up run with
  additional cancellable retries at 150 and 450 ms observed 24 of 24 complete
  test changes, versus 23 of 24 in the first probe. The slowest observed
  response was 1,281 ms versus 1,859 ms; medians were 414 ms versus 375 ms.
  These small sequential samples with polling timeouts do not establish an
  optical latency improvement. A new camera recording is needed to compare
  visible response under similar conditions.
- A second tablet recording after the retry change was analyzed using the same
  0.15 red/blue threshold and video frame timestamps. For both recordings,
  the first two setup cuts and the final incomplete interval were excluded;
  the known three-second pattern cadence also excluded two short false screen
  detections in the second recording. Before: 39 of 41 cuts matched, median
  333 ms, p95 1,899 ms, maximum 1,933 ms, and four matched delays exceeded one
  second. After: 57 of 57 cuts matched, median 400 ms, p95 900 ms, maximum
  1,033 ms, and one matched delay exceeded one second. Both recordings were
  sampled at about 30 fps and the camera position changed slightly; automatic
  shutter/exposure and wall spill remain possible. These sequential runs
  suggest fewer missed changes and shorter long delays, but do not prove the
  retry change caused the difference. The median was higher in the later run,
  and no broad color-accuracy claim follows from red/blue tests.

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
- An eight-second profile of one-shot Windows Graphics Capture calls spent
  approximately 7.38 seconds of 8.03 seconds in the library's staging-surface
  `map` operation (118 returned frames). This points to GPU readback waiting
  on this setup; it does not identify the driver-level cause. An exploratory
  45-call comparison measured mean acquisition times of 45.9 and 57.1 ms for
  full-screen batches versus 29.7 ms for a 480×270 cropped batch. The batches
  had different numbers of new frames and different screen content, so this
  is not a controlled estimate of the benefit of resizing. Cropping was not
  adopted because it omits most of the display. GPU-side downsampling or
  asynchronous readback could be investigated while retaining the entire
  screen, but neither has been implemented or measured here.
  Microsoft documents that Direct3D's
  [`D3D11_MAP_FLAG_DO_NOT_WAIT`](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/ne-d3d11-d3d11_map_flag)
  returns a busy error instead of blocking when the GPU still uses a resource.
  A future asynchronous readback would need to retain pending staging buffers,
  handle that busy result, and discard stale completed frames. Simply skipping
  a busy map without retaining its pending copy could lose every frame; such a
  change needs real-device freshness and latency testing.
- A local, experimental three-buffer Direct3D readback prototype used
  `D3D11_MAP_FLAG_DO_NOT_WAIT` with the full 1920×1080 HDMI frame. In one
  12-second run it completed 222 readbacks, with an average pending-copy age
  of 74.4 ms and p95 201.5 ms. The average polling-loop time was 0.86 ms
  (p95 3.23 ms), but this does **not** mean frames were ready in that time.
  A first run had a COM cleanup error; a second completed without the error.
  These runs used private capture-library interfaces and did not establish
  an optical latency improvement. The prototype was not integrated. A safe
  implementation would also need to validate pixel correctness, frame
  freshness, GPU resource ownership, display recovery, and shutdown under
  load across supported machines.
- On a mostly static desktop, an isolated WinRT `grab` test measured roughly
  12 ms per call with the library's default new-frame wait and 0.05 ms with
  `new_frame_only=False`. An application run using cached-frame mode reported
  mean capture 3.7 ms and p95 4.8 ms for its rolling samples. A separate
  alternating red/blue window test observed median first-matching capture
  times of 47 ms in both modes (19 changes per mode); mean grab-call times
  were 13.7 ms with the default and 0.6 ms with cached-frame mode. These are
  small sequential experiments, not an optical latency comparison. Cached
  mode was enabled for the WinRT backend; the capture library is still asked
  for a new frame on each call, with its cached frame used if none is ready.
- A subsequent pair of 75-second, three-second red/blue pattern runs compared
  sender and bulb-status timings before and after cached-frame mode. Each had
  24 complete matching updates and used frequent `getPilot` polling. Median
  time from the pattern-change call to the first local send was 63 ms before
  and 86 ms after; p95 was 94 and 125 ms. Median time to the first matching
  status response was 414 and 523 ms, respectively. The runs were sequential,
  had polling timeouts, and used the pattern call rather than an optical
  screen timestamp. They do not establish a latency improvement and also do
  not isolate cached-frame mode from changes in system or network load.
- A later full-screen, changing-pattern test compared the selected WinRT
  backend with MSS on this monitor. Both matched 19 sampled changes with a
  47 ms median from the pattern call to the first matching capture. WinRT's
  mean grab call was 1.0 ms versus 23.7 ms for MSS in these sequential runs,
  so MSS was not promoted as the default. A separate synthetic 640×360
  letterbox-mask benchmark measured the original calculation at 2.0–2.2 ms
  per frame and an allocation-reduced equivalent at 1.3–1.4 ms on this PC.
  This microbenchmark does not establish a screen-to-light improvement.
- On Windows/Python 3.10, a 10 ms `sleep` measured roughly 15.5 ms median on
  this PC. A high-resolution waitable timer measured approximately 10.3 ms.
  In two 24 FPS synthetic engine runs, ordinary sleep produced about 46.5–46.7
  ms median frame intervals versus 42.0 ms with the timer. A sequential live
  WinRT capture check processed approximately 20.7 versus 23.3 frames per
  second, including startup time, with no capture error. The timer is scoped
  to the active sync worker and falls back to ordinary sleep when unavailable;
  it is closed on worker exit. More frequent wakeups can increase power use.
  These checks do not establish a screen-to-light or color-accuracy improvement.

## Color observations

A further tablet recording showed the lamp visibly changing for red, green,
blue, cyan, magenta, yellow, and white test screens. The white reflection
appeared warmer than the monitor. The video clipped one or more screen
channels in cyan, magenta, yellow, green, and white; the sampled wall region
also clipped on white. Camera processing, screen spill and wall reflectance
remain uncalibrated, so these images cannot determine an accurate white mix
or establish absolute color accuracy. No color calibration was applied.

A subsequent ISO 100 / 5000 K white-balance recording compared the same
single-white-channel bulb with five direct RGBW payloads: warm-white-only
`(0,0,0,255)`, RGB-only `(255,255,255,0)`, mixed A `(180,255,180,96)`,
mixed B `(180,255,180,160)`, and mixed C `(200,255,200,64)`. In the video,
warm-white-only appeared yellow/orange, RGB-only appeared violet, and the
mixed settings appeared between those extremes. On one unclipped monitor ROI
and one unclipped wall ROI, the simple distance between their normalized
camera RGB values was 0.260 for warm-only, 0.197 for RGB-only, 0.083 for A,
0.066 for B, and 0.120 for C. This is an exploratory camera-space metric,
not a calibrated color difference: wall reflectance, monitor spectral output,
automatic shutter/exposure and video processing are not characterized.
Mixed B was also visibly dimmer than warm-white-only. Some screen regions
still clipped, although the two sampled regions did not. No default or
device calibration was changed based on this comparison.

After reviewing the dimmer/neutral trade-off, the user selected mixed B for
the tested bulb. A separate, user-selected neutral-white payload is now saved
locally for that bulb, and the mapper blends it into near-neutral colors.
This is a preference based on the camera comparison, not a colorimetric
calibration. A live app test on a white screen reported the selected RGBW
payload `(180,255,180,160)` and the bulb's `getPilot` reported those same
channels. The lamp's previous state was restored. Saturated colors continue
through the ordinary mapping path.

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

The color sampling grid was later changed to cover the full captured region
when frame dimensions are not multiples of the sample count. Synthetic tests
cover unequal-sized and tiny images. A live Windows Graphics Capture test of
four colored HDMI-screen quadrants observed the expected color in each sampled
quadrant. A local 32×32-grid sampling microbenchmark averaged about 24 µs per
call. This does not verify optical color accuracy or eliminate all aliasing in
the earlier full-frame downsampling step. The lamp was off for this check.

A subsequent HDMI test used repeating one-pixel vertical stripes: one red
stripe followed by two blue stripes. The full captured frame averaged about
`(85, 0, 170)` in encoded RGB, but fixed-phase downsampling made the prior
cinematic target almost entirely red. A repeatable distributed subpixel
sampler and a blend for two prominent colors reduced that bias. In an app
test with the actual HDMI monitor and lamp, the target was `(223, 1, 255)`,
the mapper sent `(206, 2, 254, 0)` RGBW, and `getPilot` reported those
channels. The previous lamp state was restored. This verifies a controlled
screen-to-command path, not the optical output or arbitrary patterns.

On this PC, synthetic 1920×1080 BGRA-to-RGB sampling averaged approximately
3.0 ms with distributed sampling, compared with about 0.7–0.9 ms for the
previous fixed-stride conversion in separate runs. That is an explicit CPU
cost for less aliasing, not a demonstrated latency improvement. Distributed
sampling is not full area averaging and can still alias some patterns; a
GPU-side area reduction remains a candidate for future evaluation.
Changing the cached coordinates to flat pixel indices later reduced the
same local 1080p conversion benchmark from about 3.0 ms to about 2.0 ms per
frame. Pixel-equivalence tests cover contiguous and non-contiguous inputs,
and another HDMI stripe capture produced a mixed red/blue target. The lamp
was unreachable during that follow-up; no new optical delay was measured.

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
