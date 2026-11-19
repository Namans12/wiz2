"""Check current engine control effects using synthetic frames, without devices."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.color_science import lerp_oklab8, oklab_distance8  # noqa: E402
from src.features.screen_sync_v2 import OptimizedCaptureConfig, OptimizedScreenSync  # noqa: E402


def _engine(**settings) -> OptimizedScreenSync:
    return OptimizedScreenSync(lambda _: None, OptimizedCaptureConfig(use_gpu=False, **settings))


def _frame_color(frame: np.ndarray, **settings) -> tuple[tuple[int, int, int], int]:
    engine = _engine(**settings)
    colors = engine._extract_colors(frame)
    return colors.get("all", (0, 0, 0)), engine.output_brightness


def measure() -> dict[str, object]:
    vivid = np.full((96, 96, 3), (170, 100, 50), dtype=np.uint8)
    dark = np.full((96, 96, 3), (40, 28, 16), dtype=np.uint8)
    vivid_low, _ = _frame_color(vivid, color_boost=1.0)
    vivid_high, _ = _frame_color(vivid, color_boost=1.7)
    _, dark_low = _frame_color(dark, min_brightness=0)
    _, dark_high = _frame_color(dark, min_brightness=60)
    fps = []
    for cap in (12, 28):
        engine = _engine(min_fps=4, max_fps=cap, adaptive_fps=True)
        engine._update_adaptive_fps(0.05)
        fps.append(engine.current_fps)
    outputs = []
    for smoothing in (0.08, 0.55):
        engine = _engine(smoothing=smoothing)
        factor = engine._target_smoothing(0.01, 1.0 / 24)
        outputs.append(lerp_oklab8((0, 0, 0), vivid_high, factor))
    smooth_delta = oklab_distance8(outputs[0], outputs[1])
    punch_delta = oklab_distance8(vivid_low, vivid_high)
    checks = {
        "responsiveness": {"lowFps": fps[0], "highFps": fps[1], "metricDelta": fps[1] - fps[0], "passes": fps[1] > fps[0]},
        "smoothness": {"fastOutput": outputs[0], "slowOutput": outputs[1], "metricDelta": smooth_delta, "passes": smooth_delta > 0.01},
        "colorPunch": {"lowBoostOutput": vivid_low, "highBoostOutput": vivid_high, "metricDelta": punch_delta, "passes": punch_delta > 0.001},
        "darkSceneFloor": {"lowDimming": dark_low, "highDimming": dark_high, "metricDelta": dark_high - dark_low, "passes": dark_high > dark_low},
    }
    return {
        "scope": "synthetic current-engine control effects; not GUI wiring, live cadence or hardware accuracy",
        "summary": {"passed": all(check["passes"] for check in checks.values()), "visibleControlsChecked": len(checks)},
        "checks": checks,
    }


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    report = measure()
    print(json.dumps(report, indent=2))
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
