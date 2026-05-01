# Hardware Color Accuracy Testing

This repo has normal unit tests for color mapping and an opt-in hardware test for the real WiZ bulb plus camera loop.

## Setup

1. Copy `hardware_targets.example.json` to `hardware_targets.json`.
2. Set `video` to a local test video with bright scene changes.
3. Set each target bulb `ip`.
4. Set `camera` to the OpenCV camera index.
5. Set `roi` to `[x, y, width, height]` around the visible bulb/light area in the camera frame.

`hardware_targets.json` is intentionally ignored by git because it contains local IPs, camera layout, and room-specific ROI values.

## Dry Run

```powershell
python scripts/run_hardware_color_accuracy.py --manifest hardware_targets.json
```

This prints the exact verifier commands without touching the bulb.

## Real Run

```powershell
python scripts/run_hardware_color_accuracy.py --manifest hardware_targets.json --execute
```

The runner calls `scripts/verify_video_color_mapping.py` for each configured bulb, saves annotated scene images plus JSON reports under `output/hardware-runs/<timestamp>/`, then fails if the measured perceptual errors exceed the configured thresholds.

## Pytest Gate

```powershell
$env:RUN_HARDWARE_TESTS = "1"
python -m pytest -m hardware
```

Without `RUN_HARDWARE_TESTS=1`, the real bulb test is skipped, so normal CI and local unit tests stay fast and hardware-free.
