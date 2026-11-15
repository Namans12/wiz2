# Latency And Accuracy Testing

These tools separate measurable facts from subjective “feels faster” testing.

## Synthetic Pipeline Benchmark

Run a deterministic benchmark of the hot path:

```powershell
python scripts\benchmark_screen_sync.py --iterations 200 --width 1920 --height 1080
```

The report includes:

- synthetic capture frame creation
- color extraction
- color mapping
- smoothing
- packet encoding
- total pipeline time

Use this before and after algorithm changes. The useful target is keeping Python processing comfortably below the WiZ firmware/network cap, not forcing send rate above roughly 28 Hz.

## Control Effect Test

Run:

```powershell
python scripts\measure_control_effects.py
```

This uses fixed synthetic scenes and verifies that visible controls produce measurable output changes:

- Responsiveness changes requested FPS.
- Smoothness changes transition distance.
- Color Punch changes mapped output color.
- Dark Scene Floor lifts dim-scene output.

If a visible control fails this test, hide it in Advanced/Diagnostics or remove it.

## Fake UDP Bulb

Run a local receiver:

```powershell
python scripts\fake_wiz_bulb_udp.py --host 127.0.0.1 --port 38899 --seconds 20
```

Point a test sender at `127.0.0.1`. The receiver validates the `setPilot`
packet and its RGB and white-channel values. Add `--rgb-only` when a specific
test should require both white channels to be zero.

## Real End-To-End Latency

### Capture FPS versus CPU

`python scripts/benchmark_capture_modes.py --monitor 2 --seconds 12 --out capture.json`
opens a repeatable color-cut pattern on the chosen monitor without controlling
lamps. It compares adaptive 15–30 FPS with fixed 15, 30 and 60 FPS, then repeats
in reverse order. Each run gets a two-second capture warmup. The report records
the effective configured FPS, processed FPS, CPU seconds, captured output
matches and cut-response times. Escape closes the window.

These timings run from the Tk redraw request to matching engine output, not
from monitor photons to lamp photons. Pattern rendering contributes to CPU
use. Repeat runs and examine outliers; do not change production defaults based
on one favorable median. The optimized engine supports fixed 60 FPS; sender
rate limits remain independent.

### Two-lamp reported-state checks

Run `python scripts/validate_live_link.py --ip FIRST_IP --ip SECOND_IP --out report.json`.
This opt-in test changes both lamps, then restores and checks their starting
states. It exercises per-zone brightness with twelve colors including gray,
dark gray and black, compares link-off/link-on in ABBA order, changes a
confirmed lamp state to test recovery without a new frame, and runs a bounded
two-lamp soak (`--soak-seconds`, default 120). Do not run another sync sender
at the same time. Use current addresses obtained from discovery.

The comparison uses identical observer queries in both modes; those queries
can themselves affect radio sleep. Results are dispatch-to-reported-state
timings, not optical latency, perceived brightness or measured color accuracy.
Keep unmatched cases and restoration failures in the report.

The low-latency monitor continues checking confirmed states. A sustained
mismatch reopens confirmation; a transient mismatched reply does not. After
four recovery attempts it waits one second before another bounded group.
Recovery counts and timings are separate from initial update confirmation.
Stopping sync stops this recovery, allowing other controllers to take over.

True latency must include screen, app, WiFi, bulb firmware, and the bulb LED response. The recommended lab setup is:

1. Put the screen and bulb in the same camera frame.
2. Play a generated video with hard color cuts and visible timestamps.
3. Record at 60 fps or higher.
4. Measure frame difference between screen color change and bulb color change.
5. Repeat on quiet WiFi and congested WiFi.

Report median, p95, and worst-case latency. Do not rely only on Python timing for product claims.

For a constant-frame-rate recording of alternating red and blue cuts, choose
separate rectangles for the screen and lamp, then run:

```powershell
python scripts\analyze_screen_light_latency.py recording.mp4 --screen-roi 20,20,200,150 --light-roi 300,20,80,80 --fps 60
```

Replace the ROI coordinates and FPS with the values for the actual recording.
The tool reports matched and unmatched cuts as well as the frame interval.
Its estimates are threshold-based camera observations: dropped frames, rolling
shutter, changing exposure and variable frame rate can bias them. Inspect the
video and verify that both ROIs show the intended changes; collect enough cuts
before comparing latency distributions.

For a live camera that does not report a trustworthy frame rate, use frame
arrival timestamps instead. With the separate red/blue test pattern running:

```powershell
python scripts\measure_screen_light_camera.py --camera 2 --screen-roi 500,210,190,100 --light-roi 510,15,100,65 --seconds 45
```

Set the camera index and rectangles for the current setup. This records only
region scores in memory, not video. Virtual-camera buffering and exposure can
still distort the result; the reported frame intervals are not sensor timestamps.

## Real Color Accuracy

Use the hardware gate:

```powershell
python scripts\run_hardware_color_accuracy.py --manifest hardware_targets.json --execute
```

For production claims, test at least:

- red, green, blue
- cyan, magenta, yellow
- warm low-saturation scenes
- cool low-saturation scenes
- near-black scenes
- fast color cuts

Camera calibration and room lighting must remain fixed between runs.
