"""Time current software processing with streamed synthetic frames, never capture."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Callable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.color_mapping import BulbColorProfile, BulbGamutMapper  # noqa: E402
from src.core.color_science import lerp_oklab8  # noqa: E402
from src.features.fast_udp_sender import encode_set_pilot_payload  # noqa: E402
from src.features.screen_sync_v2 import OptimizedCaptureConfig, OptimizedScreenSync  # noqa: E402


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
    if iterations < 1 or width < 1 or height < 1:
        raise ValueError("iterations, width and height must be positive")
    config = OptimizedCaptureConfig(sample_size=sample_size, use_gpu=False)
    engine = OptimizedScreenSync(lambda _: None, config)
    mapper, mapper_setup_ms = _time_call(lambda: BulbGamutMapper(BulbColorProfile(white_channels=2)))
    current = None
    stage_times: dict[str, list[float]] = {
        stage: [] for stage in ("frameProcessingMs", "mappingMs", "smoothingMs", "packetEncodeMs", "processingTotalMs")
    }
    frame_generation_ms = 0.0
    for index in range(iterations):
        # Frames are streamed, so memory does not grow with iteration count.
        frame, elapsed = _time_call(lambda: _synthetic_frame(index, width, height))
        frame_generation_ms += elapsed
        total_started = time.perf_counter()
        colors, elapsed = _time_call(lambda: engine._extract_colors(frame))
        stage_times["frameProcessingMs"].append(elapsed)
        target = colors.get("all", current or (0, 0, 0))
        factor = engine._target_smoothing(0.01, 1.0 / config.max_fps)
        current, elapsed = _time_call(lambda: target if current is None else lerp_oklab8(current, target, factor))
        stage_times["smoothingMs"].append(elapsed)
        payload, elapsed = _time_call(lambda: mapper.map_rgb(current))
        stage_times["mappingMs"].append(elapsed)
        _, elapsed = _time_call(lambda: encode_set_pilot_payload(payload, brightness=engine.output_brightness))
        stage_times["packetEncodeMs"].append(elapsed)
        stage_times["processingTotalMs"].append((time.perf_counter() - total_started) * 1000)
    return {
        "scope": "synthetic software processing; excludes capture, UDP and bulb latency",
        "limitations": "Fixed motion and synthetic frame interval; does not exercise live send gates, capture or WiFi.",
        "iterations": iterations,
        "resolution": [width, height],
        "sampleSize": config.sample_size,
        "mapperSetupMs": mapper_setup_ms,
        "frameGenerationMsTotal": frame_generation_ms,
        "frameGenerationMsPerFrame": frame_generation_ms / iterations,
        "stages": {
            stage: {
                "meanMs": statistics.fmean(values),
                "p95Ms": sorted(values)[math.ceil(len(values) * 0.95) - 1],
                "maxMs": max(values),
            }
            for stage, values in stage_times.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=120)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--sample-size", type=int, default=60)
    args = parser.parse_args()
    print(json.dumps(benchmark(max(1, args.iterations), max(64, args.width), max(64, args.height), max(16, args.sample_size)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
