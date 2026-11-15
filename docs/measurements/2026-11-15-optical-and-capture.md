# Optical comparison and capture cadence — 2026-11-15

## What was fixed

- A confirmed bulb color is continuously rechecked. Sustained drift triggers
  bounded retries; exhausting the first retry group no longer disables
  recovery permanently. A lone mismatched reply is debounced.
- Immediate empty WinRT polls no longer busy-spin.
- Early WinRT polls now update the same fallback cache used by ordinary
  capture. Previously a new blue frame could be consumed by a poll, then
  replaced by the old cached red frame. A failing regression test reproduced
  this, and a subsequent live four-cut test plus user observation verified
  the fix. The first attempted optical recording was rejected.
- Fixed 60 FPS is no longer silently clamped to 30 FPS by the legacy base
  configuration. The legacy engine's own limit and UDP sender limits are
  unchanged. The user's adaptive 15–30 FPS default was retained.

## Optical recording

Source: `20261115_054700.mp4`, 1920×1080, 4,489 decoded frames, approximately
150 seconds. The user was asked to use WB 5000 K and ISO 100; camera settings
were not independently measured or certified. Actual timestamps show a
median interval of 33.322 ms, so this is approximately **30 FPS**, not 60 FPS.
The analysis uses each frame's FFprobe `best_effort_timestamp_time`, rather
than assuming constant frame intervals. Camera sampling, rolling shutter,
exposure, threshold choice and wall reflections limit precision.
The only frame interval above 40 ms was the first interval (115.467 ms),
before the measured pattern sections.

The monitor and visible lamp were recorded together during an ABBA sequence:
link off, on, on, off. Each section had twelve red/blue scenes, producing
eleven within-section transitions. All **44/44 transitions were observed**
at both sampled lamp regions. This is optical verification of the visible
lamp while both lamps were driven; it is not independent optical validation
of both lamps.

ROIs `(x, y, width, height)` in the original recording:

- Monitor: `(800, 420, 300, 180)`.
- Wall reflection: `(820, 35, 300, 100)`.
- Lampshade: `(1030, 265, 70, 25)`, away from the clipped bulb core.

For each region the signal is `(mean red - mean blue) / (mean red + mean blue)`.
Green screen intervals separate the four sections. Lamp signals are centered
and scaled using their 10th/90th percentile plateaus across those sections.
The monitor threshold is ±0.15. Lamp thresholds ±0.1 and ±0.3 are compared,
requiring two stable frames and timestamping the first qualifying frame.
The full trace confirmed distinct settled red/blue levels; cyan/magenta
preview frames were transitions, not proof of incorrect settled colors.

### Main timing result: ±0.1 lamp threshold

| Region | Link | Changes | Median | p95 | Maximum |
|---|---|---|---|---|---|
| Wall | Off | 22/22 | 450 ms | 633 ms | 800 ms |
| Wall | On | 22/22 | 300 ms | 633 ms | 633 ms |
| Shade | Off | 22/22 | 533 ms | 700 ms | 800 ms |
| Shade | On | 22/22 | 367 ms | 666 ms | 666 ms |

The two individual on sections each had a wall median of approximately 300 ms;
the off sections had approximately 433 and 500 ms. The median improvement is
consistent across both regions and both on sections. The tail improvement is
less clear: the wall p95 is unchanged in the pooled samples. This does not
prove that rare 1–2 second stalls are eliminated.

Using the stricter ±0.3 threshold shifts medians later: wall 516 → 350 ms and
shade 566 → 417 ms. All transitions are still observed. This sensitivity is
why a single precise latency number would overstate the evidence.

A separate per-transition calculation uses the local pre-cut plateau
(-0.5 to -0.1 seconds) and settled target plateau (+1.7 to +2.2 seconds).
With the link on, median 10%/50%/90% transition progress is approximately
117/267/433 ms on the wall and 117/267/467 ms on the shade. These describe
observable progression through a color change, not calibrated photon output,
and do not identify which part of the pipeline causes the remaining delay.

Raw summaries, frame timing, thresholds, per-transition results and source
video SHA-256 are in `2026-11-15-optical-analysis.json`. Capture/sender logs are
in `2026-11-15-optical-run-log.json`; all sections passed the capture-color
guard. Its ~0.6–0.9 ms reported ordinary-grab averages exclude work performed
in early polls. They must not be compared directly to earlier moving-video
capture timings or treated as total capture cost.

## Capture FPS/CPU comparison

`benchmark_capture_modes.py --monitor 2 --seconds 10` ran eight warmed-up
sections, adaptive → 15 → 30 → 60 → 60 → 30 → 15 → adaptive. CPU figures are
percent of one logical core, including Tk pattern drawing. This test sends
no commands to lamps and measures redraw-request-to-engine-output delay.

| Setting | Processed FPS, two runs | Median response, two runs | CPU, two runs |
|---|---|---|---|
| Adaptive 15–30 | 18.6 / 18.7 | 78 / 69 ms | 25% / 17% |
| Fixed 15 | 15.0 / 14.9 | 72 / 76 ms | 18% / 13% |
| Fixed 30 | 29.6 / 28.4 | 61 / 76 ms | 29% / 60% |
| Fixed 60 | 56.8 / 57.8 | 51 / 48 ms | 62% / 59% |

Every displayed cut was matched (111/111 total). One fixed-30 repeat had
unusually high CPU and a 523 ms p95, so do not generalize its tail behavior.
The first fixed-30 run used about half the CPU of fixed 60 for only about
10 ms more median delay. The data does not justify raising the default to
60 FPS; it remains an explicit choice. Raw data: `2026-11-15-capture-modes.json`.

## Verification and artifacts

- Python: **319 passed**, one hardware-marked test excluded. Separate live
  tests are documented in `2026-11-15-two-lamp-validation.md`.
- Ruff: repository checks passed.
- Flutter: **19 tests passed**, including desktop-sized TV navigation and
  phone-sized navigation.
- Android native: **13 tests passed**, explicitly rerun with
  `:app:testDebugUnitTest --rerun`, covering sender, HDR conversion, capture
  policy, refresh, regions and output policy.
- Windows app and installer rebuilt successfully. The executable survived a
  short process-start smoke check; this was not a complete packaged GUI test.

The phone was detected through ADB. No new physical phone/Google TV capture
session or macOS run was performed. Calibrated multi-color accuracy, real
video, hours-long capture sessions and controlled Wi-Fi impairment remain
open. This session tested variable weak-signal home Wi-Fi, not a reproducible
network-impairment matrix. Both lamps were restored after live tests.

Read-only `getUserConfig` checks subsequently confirmed **500 ms fade-in and
500 ms fade-out on both lamps**. These firmware settings were preserved. They
can add delay to power-state transitions around sustained black frames; they
do not by themselves explain ordinary red-to-blue transition timing. The
existing GUI warning reports this on/off fade when profile metadata is
available. A future black-return comparison should separately measure this
setting and the application's intentional black-frame persistence guard.
