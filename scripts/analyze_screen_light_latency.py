"""Estimate screen-to-light response from constant-frame-rate video.

Record the screen and bulb in the same camera frame while the screen alternates
red and blue. Supply non-overlapping ROIs as x,y,width,height. Results measure
camera-visible color transitions, not the moment a network packet is received.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

import cv2
import numpy as np


def transitions(values: list[float], threshold: float, stable_frames: int) -> list[tuple[int, int]]:
    """Detect sustained red/blue states, timestamped at their first frame."""
    current = 0
    candidate = 0
    count = 0
    result = []
    for index, value in enumerate(values):
        state = 1 if value >= threshold else -1 if value <= -threshold else 0
        if not state or state == current:
            candidate, count = 0, 0
            continue
        if state != candidate:
            candidate, count = state, 1
        else:
            count += 1
        if count >= stable_frames:
            if current:
                result.append((index - stable_frames + 1, state))
            current, candidate, count = state, 0, 0
    return result


def pair_transitions(screen, light, max_frames: int):
    """Pair each cut at most once; ignore light changes after the next cut."""
    samples = []
    used = 0
    for index, (screen_frame, state) in enumerate(screen):
        next_cut = screen[index + 1][0] if index + 1 < len(screen) else math.inf
        while used < len(light) and light[used][0] < screen_frame:
            used += 1
        match = used
        while match < len(light) and light[match][0] < next_cut:
            frame, light_state = light[match]
            if frame - screen_frame > max_frames:
                break
            if light_state == state:
                samples.append((screen_frame, frame, state))
                used = match + 1
                break
            match += 1
    return samples


def summarize_transition_delays(screen, light, times, max_latency_ms):
    """Report late changes separately instead of silently dropping their delays."""
    pairs = pair_transitions(screen, light, len(times))
    all_delays = sorted((times[b] - times[a]) * 1000 for a, b, _ in pairs)
    delays = [delay for delay in all_delays if delay <= max_latency_ms]
    late = [delay for delay in all_delays if delay > max_latency_ms]
    paired_frames = {screen_frame: light_frame for screen_frame, light_frame, _ in pairs}
    timeline = []
    for index, (screen_frame, state) in enumerate(screen):
        light_frame = paired_frames.get(screen_frame)
        delay = None if light_frame is None else (times[light_frame] - times[screen_frame]) * 1000
        end = screen[index + 1][0] if index + 1 < len(screen) else len(times) - 1
        timeline.append({
            "screen_frame": screen_frame,
            "screen_time_s": times[screen_frame] - times[0],
            "color": "red" if state == 1 else "blue",
            "light_frame": light_frame,
            "delay_ms": delay,
            "status": "unobserved" if delay is None else "late" if delay > max_latency_ms else "within_limit",
            "observation_window_ms": (times[end] - times[screen_frame]) * 1000,
        })
    return {
        "latency_limit_ms": max_latency_ms,
        "matched_transitions": len(delays),
        # Kept for compatibility: includes late and entirely unobserved changes.
        "unmatched_screen_transitions": len(screen) - len(delays),
        "late_transitions": len(late),
        "late_samples_ms": late,
        "unobserved_before_next_transition_or_end": len(screen) - len(pairs),
        "all_observed_transitions": len(all_delays),
        "all_observed_median_ms": statistics.median(all_delays) if all_delays else None,
        "all_observed_p95_ms": all_delays[math.ceil(len(all_delays) * .95) - 1] if all_delays else None,
        "all_observed_max_ms": max(all_delays) if all_delays else None,
        "transition_timeline": timeline,
        "median_ms": statistics.median(delays) if delays else None,
        "p95_ms": delays[math.ceil(len(delays) * .95) - 1] if delays else None,
        "max_ms": max(delays) if delays else None,
        "samples_ms": delays,
    }


def parse_roi(value: str) -> tuple[int, int, int, int]:
    try:
        coords = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROI must be x,y,width,height") from exc
    if len(coords) != 4 or min(coords[:2]) < 0 or min(coords[2:]) < 1:
        raise argparse.ArgumentTypeError("ROI must be x,y,width,height with positive size")
    return coords


def score(frame: np.ndarray, roi: tuple[int, int, int, int]) -> float:
    x, y, width, height = roi
    if y + height > frame.shape[0] or x + width > frame.shape[1]:
        raise ValueError("ROI extends beyond the video frame")
    blue, _, red = frame[y:y + height, x:x + width].mean(axis=(0, 1))
    if red + blue < 10:
        return 0.0
    return float((red - blue) / max(red + blue, 1.0))


def overlaps(first, second) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--screen-roi", required=True, type=parse_roi)
    parser.add_argument("--light-roi", required=True, type=parse_roi)
    parser.add_argument("--fps", type=float, help="Verified constant recording FPS; defaults to file metadata")
    parser.add_argument("--threshold", type=float, default=0.15)
    parser.add_argument("--stable-frames", type=int, default=2)
    parser.add_argument("--max-latency-ms", type=float, default=1500)
    args = parser.parse_args()
    if (not 0 < args.threshold < 1 or args.stable_frames < 1
            or not math.isfinite(args.max_latency_ms) or args.max_latency_ms <= 0):
        parser.error("threshold must be between 0 and 1; frame count and maximum latency must be positive")
    if overlaps(args.screen_roi, args.light_roi):
        parser.error("screen and light ROIs must not overlap")
    camera = cv2.VideoCapture(str(args.video))
    if not camera.isOpened():
        parser.error("could not open video")
    try:
        fps = args.fps if args.fps is not None else camera.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or fps <= 0:
            parser.error("video FPS is unavailable; supply verified --fps")
        screen_scores, light_scores = [], []
        while True:
            ok, frame = camera.read()
            if not ok:
                break
            screen_scores.append(score(frame, args.screen_roi))
            light_scores.append(score(frame, args.light_roi))
    finally:
        camera.release()
    source = transitions(screen_scores, args.threshold, args.stable_frames)
    target = transitions(light_scores, args.threshold, args.stable_frames)
    timing = summarize_transition_delays(source, target, [i / fps for i in range(len(screen_scores))], args.max_latency_ms)
    report = {
        "scope": "camera-visible red/blue transition delay; assumes constant frame rate",
        "fps": fps, "frame_interval_ms": 1000 / fps,
        "screen_transitions": len(source), "light_transitions": len(target),
        **timing,
        "low_sample_count": timing["matched_transitions"] < 5,
        "limitations": "Threshold crossing, frame sampling, rolling shutter, exposure and dropped frames affect results; verify ROI signals and constant frame rate.",
    }
    print(json.dumps(report, indent=2))
    return 0 if timing["matched_transitions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
