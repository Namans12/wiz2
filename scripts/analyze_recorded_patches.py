"""Compare settled monitor and lamp colors in a fixed-setting tablet recording."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from analyze_screen_light_latency import overlaps, parse_roi


def sample_patch(video, timestamp, roi):
    x, y, width, height = roi
    video.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
    samples, clipped = [], []
    for _ in range(5):
        ok, frame = video.read()
        if not ok or y + height > frame.shape[0] or x + width > frame.shape[1]:
            raise ValueError(f"No valid frame or region at {timestamp:.2f} s")
        rgb = frame[y:y + height, x:x + width, ::-1]
        samples.append(rgb.mean(axis=(0, 1)))
        clipped.append(np.any(rgb >= 250, axis=2).mean())
    return np.median(samples, axis=0), round(float(np.mean(clipped)), 4)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("log", type=Path, help="Run log from run_latency_ab_test.py --color-check")
    parser.add_argument("--screen-roi", required=True, type=parse_roi)
    parser.add_argument("--lamp-roi", required=True, type=parse_roi)
    parser.add_argument("--first-red", required=True, type=float, help="First red screen onset in video seconds")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    run = json.loads(args.log.read_text(encoding="utf-8"))
    if not run.get("color_check") or run.get("capture_error") or overlaps(args.screen_roi, args.lamp_roi):
        parser.error("Use a valid color-check log and separate screen and lamp regions")
    cuts = run["segments"][0]["changes"]
    if len(cuts) != 14 or any("display_rgb" not in cut for cut in cuts):
        parser.error("The patch sequence is incomplete")
    video = cv2.VideoCapture(str(args.video))
    rows = []
    try:
        for cut in cuts:
            stamp = args.first_red + cut["at"] - cuts[0]["at"] + 0.7 * run["hold"]
            screen, sc = sample_patch(video, stamp, args.screen_roi)
            lamp, lc = sample_patch(video, stamp, args.lamp_roi)
            rows.append(dict(patch=cut["colour"], seconds=round(stamp, 3),
                             screen=screen.round(2).tolist(), lamp=lamp.round(2).tolist(),
                             screen_clipped_fraction=sc, lamp_clipped_fraction=lc))
    finally:
        video.release()
    args.out.write_text(json.dumps(dict(
        scope="Camera RGB levels for settled patches. Compare within this camera position and exposure.",
        model=run["mixing_model"], video=args.video.name, screen_roi=args.screen_roi,
        lamp_roi=args.lamp_roi, first_red=args.first_red, samples=rows), indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
