"""Compare capture cadence on a monitor using repeatable visible color cuts.

This opens a borderless test window (Escape closes it). It does not send to
lamps. Timings start at the Tk redraw request, not physical display emission,
and end at the engine's first matching output. CPU includes pattern drawing.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import mss
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import Config
from src.features.screen_sync_v2 import OptimizedScreenSync, build_optimized_capture_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 120:
        parser.error("--seconds must be 5–120")
    with mss.mss() as capture:
        if not 1 <= args.monitor < len(capture.monitors):
            parser.error("Monitor is not connected")
        geometry = capture.monitors[args.monitor]
    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{geometry['width']}x{geometry['height']}+{geometry['left']}+{geometry['top']}")
    root.attributes("-topmost", True)
    root.configure(bg="#000000")
    lock = threading.Lock()
    report = {"scope": __doc__, "geometry": geometry, "runs": []}
    state = {"engine": None, "run": None, "closed": False}
    settings = copy.deepcopy(Config.load().screen_sync)
    settings.monitor, settings.mode = args.monitor, "single"
    palette = [(255, 0, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (0, 0, 255), (255, 255, 0)]
    modes = ["adaptive", "15", "30", "60", "60", "30", "15", "adaptive"]

    def output(colors):
        color = colors.get("all")
        if color is None:
            return
        with lock:
            run = state["run"]
            if run is None or not run["cuts"]:
                return
            cut = run["cuts"][-1]
            distance = sum((a - b) ** 2 for a, b in zip(color, cut["rgb"]))
            if cut["response_ms"] is None and distance < 60 ** 2:
                cut["response_ms"] = (time.perf_counter() - cut["requested_at"]) * 1000

    def close_run():
        engine = state["engine"]
        if engine is None:
            return
        run = state["run"]
        wall = time.perf_counter() - run["started"]
        cpu = time.process_time() - run["cpu_started"]
        run["snapshot"] = engine.debug_snapshot
        run["wall_seconds"], run["cpu_seconds"] = wall, cpu
        run["cpu_one_core_percent"] = cpu / wall * 100
        run["processed_fps"] = (run["snapshot"]["frames_processed"] - run.get("initial_frames", 0)) / wall
        engine.stop()
        state["engine"] = None
        with lock:
            state["run"] = None
        values = [cut["response_ms"] for cut in run["cuts"] if cut["response_ms"] is not None]
        run["matched"] = len(values)
        run["median_ms"] = float(np.median(values)) if values else None
        run["p95_ms"] = float(np.percentile(values, 95)) if values else None
        print({k: run[k] for k in ("mode", "processed_fps", "cpu_one_core_percent", "matched", "median_ms", "p95_ms")}, flush=True)

    def finish():
        state["closed"] = True
        root.destroy()

    root.bind("<Escape>", lambda _: finish())

    def start_run(index):
        if index >= len(modes):
            finish()
            return
        mode = modes[index]
        settings.adaptive_fps = mode == "adaptive"
        settings.min_fps, settings.max_fps = 15, 30 if mode == "adaptive" else int(mode)
        settings.fps = settings.max_fps
        # Allow capture backend selection to settle on a visible frame before
        # measuring. Otherwise cold-start/fallback dominates the first cut.
        root.configure(bg="#ffffff")
        root.update_idletasks()
        run = {"mode": mode, "cuts": [], "started": time.perf_counter(), "cpu_started": time.process_time()}
        report["runs"].append(run)
        state["run"] = run
        capture_config = build_optimized_capture_config(settings, ())
        run["configured_fps"] = capture_config.fps
        run["configured_adaptive"] = capture_config.adaptive_fps
        engine = OptimizedScreenSync(output, capture_config)
        state["engine"] = engine
        engine.start()

        def draw(step):
            if state["closed"]:
                return
            if time.perf_counter() - run["started"] >= args.seconds:
                close_run()
                root.configure(bg="#000000")
                root.after(800, lambda: start_run(index + 1))
                return
            rgb = palette[step % len(palette)]
            with lock:
                run["cuts"].append({"rgb": rgb, "requested_at": time.perf_counter(), "response_ms": None})
            root.configure(bg="#{:02x}{:02x}{:02x}".format(*rgb))
            root.update_idletasks()
            root.after(750, lambda: draw(step + 1))

        def begin():
            run["started"], run["cpu_started"] = time.perf_counter(), time.process_time()
            run["initial_frames"] = engine.debug_snapshot["frames_processed"]
            draw(0)

        root.after(2000, begin)

    try:
        root.after(500, lambda: start_run(0))
        root.mainloop()
    finally:
        close_run()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, default=lambda v: v.item()), encoding="utf-8")
        print("report:", args.out, flush=True)


if __name__ == "__main__":
    main()
