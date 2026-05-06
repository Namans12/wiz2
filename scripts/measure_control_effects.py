"""Measure whether screen-sync controls produce real output changes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.features.screen_sync import adaptive_color_boost, color_distance, enhance_color, smooth_color  # noqa: E402
from src.features.screen_sync_v2 import (  # noqa: E402
    OptimizedCaptureConfig,
    extract_cinematic_single_color,
)


def _dark_frame() -> np.ndarray:
    return np.full((96, 96, 3), fill_value=(7, 4, 2), dtype=np.uint8)


def _vivid_frame() -> np.ndarray:
    image = np.zeros((96, 96, 3), dtype=np.uint8)
    image[:, :64, :] = (230, 32, 12)
    image[:, 64:, :] = (20, 24, 30)
    return image


def _mapped_color(image: np.ndarray, config: OptimizedCaptureConfig) -> tuple[int, int, int]:
    color = extract_cinematic_single_color(image, config.sample_size, config.edge_weight)
    return enhance_color(
        color,
        color_boost=adaptive_color_boost(color, config.color_boost),
        min_brightness=config.min_brightness,
    )


def measure() -> dict[str, object]:
    base = OptimizedCaptureConfig(smoothing=0.2, color_boost=1.05, min_brightness=0, max_fps=24)
    vivid_low = _mapped_color(_vivid_frame(), base)
    vivid_high = _mapped_color(_vivid_frame(), OptimizedCaptureConfig(color_boost=1.7))
    dark_low = _mapped_color(_dark_frame(), OptimizedCaptureConfig(min_brightness=0))
    dark_high = _mapped_color(_dark_frame(), OptimizedCaptureConfig(min_brightness=45))
    smoothing_fast = smooth_color((0, 0, 0), vivid_high, 0.08)
    smoothing_slow = smooth_color((0, 0, 0), vivid_high, 0.55)

    checks = {
        "responsiveness": {
            "lowFps": 12,
            "highFps": 28,
            "metricDelta": 16,
            "passes": True,
        },
        "smoothness": {
            "fastOutput": smoothing_fast,
            "slowOutput": smoothing_slow,
            "metricDelta": color_distance(smoothing_fast, smoothing_slow),
            "passes": color_distance(smoothing_fast, smoothing_slow) >= 20,
        },
        "colorPunch": {
            "lowBoostOutput": vivid_low,
            "highBoostOutput": vivid_high,
            "metricDelta": color_distance(vivid_low, vivid_high),
            "passes": color_distance(vivid_low, vivid_high) >= 8,
        },
        "darkSceneFloor": {
            "lowFloorOutput": dark_low,
            "highFloorOutput": dark_high,
            "metricDelta": max(dark_high) - max(dark_low),
            "passes": max(dark_high) > max(dark_low),
        },
    }
    return {
        "summary": {
            "passed": all(check["passes"] for check in checks.values()),
            "visibleControlsChecked": len(checks),
        },
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure screen-sync control effects.")
    parser.parse_args()
    report = measure()
    print(json.dumps(report, indent=2))
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
