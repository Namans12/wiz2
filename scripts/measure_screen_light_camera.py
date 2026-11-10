"""Measure red/blue screen-to-light transitions using timestamped camera frames.

Point a camera at the screen and its light, then run a separate alternating
red/blue screen pattern. This tool reads the camera only; it does not change
the screen, lamp or configuration, and does not store video frames.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.analyze_screen_light_latency import overlaps, pair_transitions, parse_roi, score, transitions  # noqa: E402


def summarize(samples, threshold=0.15, stable_frames=2, max_latency_ms=1500):
    """Use actual frame-arrival timestamps rather than an assumed video FPS."""
    times = [sample[0] for sample in samples]
    if any(not math.isfinite(t) for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("frame timestamps must be finite and strictly increasing")
    screen = transitions([sample[1] for sample in samples], threshold, stable_frames)
    light = transitions([sample[2] for sample in samples], threshold, stable_frames)
    pairs = pair_transitions(screen, light, len(samples))
    delays = sorted((times[b] - times[a]) * 1000 for a, b, _ in pairs
                    if (times[b] - times[a]) * 1000 <= max_latency_ms)
    intervals = [(b - a) * 1000 for a, b in zip(times, times[1:])]
    return {
        "scope": "camera-visible threshold crossing; timestamps are frame arrivals, not sensor exposure times",
        "frames": len(samples), "screen_transitions": len(screen),
        "matched_transitions": len(delays), "unmatched_screen_transitions": len(screen) - len(delays),
        "median_ms": statistics.median(delays) if delays else None,
        "p95_ms": delays[math.ceil(len(delays) * .95) - 1] if delays else None,
        "max_ms": max(delays) if delays else None,
        "frame_interval_median_ms": statistics.median(intervals) if intervals else None,
        "frame_interval_max_ms": max(intervals) if intervals else None,
        "threshold": threshold, "stable_frames": stable_frames,
        "samples_ms": delays,
        "limitations": "Camera exposure, rolling shutter, virtual-camera buffering and screen spill may bias results. Camera color accuracy is not calibrated.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, required=True)
    parser.add_argument("--screen-roi", type=parse_roi, required=True)
    parser.add_argument("--light-roi", type=parse_roi, required=True)
    parser.add_argument("--seconds", type=float, default=45)
    parser.add_argument("--threshold", type=float, default=.15)
    parser.add_argument("--stable-frames", type=int, default=2)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0 or not 0 < args.threshold < 1 or args.stable_frames < 1:
        parser.error("duration and stable frames must be positive and threshold must be between 0 and 1")
    if overlaps(args.screen_roi, args.light_roi):
        parser.error("screen and light ROIs must not overlap")
    camera = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY)
    if not camera.isOpened():
        parser.error("could not open camera")
    samples = []
    try:
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            ok, frame = camera.read()
            received = time.monotonic()
            if not ok or frame is None:
                raise RuntimeError("camera stopped returning frames")
            samples.append((received, score(frame, args.screen_roi), score(frame, args.light_roi)))
    finally:
        camera.release()
    report = summarize(samples, threshold=args.threshold, stable_frames=args.stable_frames)
    print(json.dumps(report, indent=2))
    return 0 if report["matched_transitions"] >= 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
