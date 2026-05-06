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

Point a test sender at `127.0.0.1`. The receiver validates that packets are JSON `setPilot` messages and that RGB screen-sync packets explicitly include `c:0` and `w:0`.

## Real End-To-End Latency

True latency must include screen, app, WiFi, bulb firmware, and the bulb LED response. The recommended lab setup is:

1. Put the screen and bulb in the same camera frame.
2. Play a generated video with hard color cuts and visible timestamps.
3. Record at 60 fps or higher.
4. Measure frame difference between screen color change and bulb color change.
5. Repeat on quiet WiFi and congested WiFi.

Report median, p95, and worst-case latency. Do not rely only on Python timing for product claims.

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
