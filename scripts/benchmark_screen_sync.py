"""Benchmark screen-sync processing stages with deterministic synthetic frames."""

from __future__ import annotations

import argparse
import json
import sys
import statistics
import time
from pathlib import Path
from typing import Callable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.features.fast_udp_sender import encode_set_pilot_payload  # noqa: E402
from src.features.screen_sync import adaptive_color_boost, enhance_color, smooth_color  # noqa: E402
from src.features.screen_sync_v2 import (  # noqa: E402
    OptimizedCaptureConfig,
    extract_cinematic_single_color,
)


def _synthetic_frame(index: int, width: int, height: int) -> np.ndarray:
    x = np.linspace(0, 255, width, dtype=np.uint16)
    y = np.linspace(0, 255, height, dtype=np.uint16)
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = ((x[None, :] + index * 7) % 255).astype(np.uint8)
    frame[:, :, 1] = ((y[:, None] + index * 5) % 255).astype(np.uint8)
    frame[:, :, 2] = (((x[None, :] // 2 + y[:, None] // 2) + index * 3) % 255).astype(np.uint8)
    return frame


def _time_call(fn: Callable[[], object]) -> tuple[object, float]:
    started = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - started) * 1000


def benchmark(iterations: int, width: int, height: int, sample_size: int) -> dict[str, object]:
    config = OptimizedCaptureConfig(sample_size=sample_size)
    current = (0, 0, 0)
    stage_times: dict[str, list[float]] = {
        "extractMs": [],
        "mappingMs": [],
        "smoothingMs": [],
        "packetEncodeMs": [],
        "processingTotalMs": [],
    }
    frames, frame_generation_ms = _time_call(
        lambda: [_synthetic_frame(index, width, height) for index in range(iterations)]
    )

    for frame in frames:
        total_started = time.perf_counter()

        color, elapsed = _time_call(
            lambda: extract_cinematic_single_color(frame, config.sample_size, config.edge_weight)
        )
        stage_times["extractMs"].append(elapsed)

        mapped, elapsed = _time_call(
            lambda: enhance_color(
                color,
                color_boost=adaptive_color_boost(color, config.color_boost),
                min_brightness=config.min_brightness,
            )
        )
        stage_times["mappingMs"].append(elapsed)

        current, elapsed = _time_call(lambda: smooth_color(current, mapped, config.smoothing))
        stage_times["smoothingMs"].append(elapsed)

        _, elapsed = _time_call(lambda: encode_set_pilot_payload((*current, 0)))
        stage_times["packetEncodeMs"].append(elapsed)
        stage_times["processingTotalMs"].append((time.perf_counter() - total_started) * 1000)

    return {
        "iterations": iterations,
        "resolution": [width, height],
        "sampleSize": sample_size,
        "frameGenerationMsTotal": frame_generation_ms,
        "frameGenerationMsPerFrame": frame_generation_ms / iterations,
        "stages": {
            stage: {
                "meanMs": statistics.fmean(values),
                "p95Ms": sorted(values)[max(0, int(len(values) * 0.95) - 1)],
                "maxMs": max(values),
            }
            for stage, values in stage_times.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark screen-sync processing stages.")
    parser.add_argument("--iterations", type=int, default=120)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--sample-size", type=int, default=60)
    args = parser.parse_args()

    report = benchmark(
        iterations=max(1, args.iterations),
        width=max(64, args.width),
        height=max(64, args.height),
        sample_size=max(16, args.sample_size),
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
