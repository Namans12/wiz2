"""Red/blue screen pattern with screen sync running, for a camera recording.

Shows a full-screen pattern on the synced monitor that alternates red and blue,
while the real sync engine drives the bulbs. The run is split into segments
that turn the low-latency link off (A) or on (B), separated by a few seconds of
green, so one continuous video of the screen and the light can compare both.

Record the monitor and the lit wall or lamp in the same camera frame, then
analyse each segment with scripts/analyze_screen_light_latency.py.

The bulbs' previous states are restored at the end. Escape stops early.
"""

from __future__ import annotations

import argparse
import copy
import json
import socket
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mss  # noqa: E402

from src.core.async_runtime import BackgroundAsyncLoop  # noqa: E402
from src.core.bulb_controller import BulbController  # noqa: E402
from src.core.config import Config  # noqa: E402
from src.features.fast_udp_sender import WIZ_PORT, FastBulbSenderPool  # noqa: E402
from src.features.screen_sync import build_bulb_color_map  # noqa: E402
from src.features.screen_sync_v2 import OptimizedScreenSync, build_optimized_capture_config  # noqa: E402
from src.features.sync_transport import (  # noqa: E402
    ScreenSyncTransport,
    brightness_for_bulbs,
    prepare_screen_sync_mapping,
)

RED, BLUE, GREEN, BLACK = "#ff0000", "#0000ff", "#00ff00", "#000000"


def _ask(ip: str, message: dict, attempts: int = 4) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.5)
    try:
        sock.connect((ip, WIZ_PORT))
        for _ in range(attempts):
            try:
                sock.send(json.dumps(message).encode())
                return json.loads(sock.recv(4096))
            except (OSError, ValueError):
                continue
    finally:
        sock.close()
    return {}


def _restore_params(prior: dict) -> dict:
    if not prior.get("state"):
        return {"state": False}
    if prior.get("sceneId"):
        return {"state": True, "sceneId": prior["sceneId"], "dimming": prior.get("dimming", 100)}
    params = {"state": True}
    for key in ("r", "g", "b", "c", "w", "temp", "dimming"):
        if key in prior:
            params[key] = prior[key]
    return params


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", action="append", help="Bulb address (repeatable). Default: saved bulbs that answer.")
    parser.add_argument("--monitor", type=int, help="Monitor to show the pattern on and sync from (default: saved setting)")
    parser.add_argument("--hold", type=float, default=3.0, help="Seconds per colour (default: 3)")
    parser.add_argument("--cuts", type=int, default=14, help="Colour changes per segment (default: 14)")
    parser.add_argument("--order", default="ABBA", help="Segments: A = link off, B = link on (default: ABBA)")
    parser.add_argument("--countdown", type=float, default=8.0, help="Seconds of black before the first segment")
    parser.add_argument("--out", type=Path, help="Write the run log as JSON")
    parser.add_argument("--color-check", action="store_true",
                        help="Display a fixed sequence of color patches with the monitored link on")
    parser.add_argument("--model", choices=("physical", "heuristic"), help="Override color model for this run")
    args = parser.parse_args()
    order = args.order.upper()
    patches = ["#ff0000", "#00ff00", "#0000ff", "#00ffff", "#ff00ff", "#ffff00",
               "#ff8000", "#ffb090", "#808080", "#a09d9b", "#9ba0a0", "#ffffff", "#202020", "#000000"]
    if args.color_check:
        order, args.cuts = "B", len(patches)
    if not order or set(order) - {"A", "B"} or not 0.5 <= args.hold <= 30 or not 2 <= args.cuts <= 200:
        parser.error("--order uses only A and B; --hold is 0.5-30 s; --cuts is 2-200")

    config = Config.load()
    settings = copy.deepcopy(config.screen_sync)
    if args.model:
        settings.mixing_model = args.model
    if args.monitor is not None:
        settings.monitor = args.monitor
    with mss.mss() as capture:
        monitors = capture.monitors
        geometry = monitors[min(max(1, settings.monitor), len(monitors) - 1)] if len(monitors) > 1 else monitors[0]

    candidates = args.ip or [bulb.ip for bulb in config.screen_sync_bulbs()]
    priors = {ip: _ask(ip, {"method": "getPilot", "params": {}}).get("result") for ip in candidates}
    ips = [ip for ip, prior in priors.items() if prior]
    if not ips:
        print("No bulb answered. Pass --ip with the bulb's current address.")
        return
    print(f"Bulbs: {', '.join(ips)} | monitor {settings.monitor} ({geometry['width']}x{geometry['height']})")

    controller = BulbController()
    runner = BackgroundAsyncLoop()
    runner.run(prepare_screen_sync_mapping(controller, ips, settings), timeout=10.0)
    holder: dict = {"transport": None}
    engine_holder: dict = {}

    def on_color_change(colors_by_target):
        transport, engine = holder["transport"], engine_holder.get("engine")
        if transport is None or engine is None:
            return
        colors = build_bulb_color_map(ips, colors_by_target, "single", {})
        transport.send(colors, brightness_for_bulbs(engine, ips, "single", {}))

    settings.mode = "single"
    engine = OptimizedScreenSync(on_color_change=on_color_change, config=build_optimized_capture_config(settings, ()))
    engine_holder["engine"] = engine

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{geometry['width']}x{geometry['height']}+{geometry['left']}+{geometry['top']}")
    root.attributes("-topmost", True)
    root.configure(bg=BLACK)
    label = tk.Label(root, font=("Arial", 20), bg=BLACK, fg="#808080")
    label.place(relx=0.5, rely=0.92, anchor="center")
    log: dict = {"order": order, "hold": args.hold, "ips": ips, "segments": [],
                 "mixing_model": settings.mixing_model, "color_check": args.color_check, "capture_error": None}
    stopped = {"value": False}

    def show(colour: str, text: str) -> None:
        root.configure(bg=colour)
        label.configure(bg=colour, text="" if args.color_check else text, fg="#ffffff" if colour != GREEN else "#000000")

    def open_segment(kind: str) -> dict:
        holder["transport"] = ScreenSyncTransport(
            controller, runner.submit,
            FastBulbSenderPool(max_send_hz=28, prewarm_ips=ips, keep_awake=(kind == "B")),
        )
        segment = {"kind": kind, "link": kind == "B", "changes": [], "started": time.perf_counter()}
        log["segments"].append(segment)
        return segment

    def close_segment(segment: dict) -> None:
        transport, holder["transport"] = holder["transport"], None
        if transport is None:
            return
        segment["link_stats"] = transport.fast_sender.link_stats
        segment["sender_stats"] = transport.fast_sender.stats
        snapshot = engine.debug_snapshot
        segment["engine"] = {key: snapshot.get(key) for key in (
            "capture_method", "capture_time_ms", "processing_time_ms", "cuts_detected", "jump_snaps", "early_wakes",
        )}
        transport.close()

    def finish() -> None:
        stopped["value"] = True
        root.destroy()

    root.bind("<Escape>", lambda _event: finish())

    def run_segment(index: int) -> None:
        if stopped["value"]:
            return
        if index >= len(order):
            finish()
            return
        kind = order[index]
        segment = open_segment(kind)

        def step(cut: int) -> None:
            if stopped["value"]:
                return
            if segment["changes"]:
                # What the engine saw by the end of the colour just shown:
                # confirms the pattern covered the captured monitor.
                seen = engine.debug_snapshot["target_colors"].get("all")
                segment["changes"][-1]["engine_target_at_end"] = list(seen) if seen else None
                if args.color_check:
                    expected = tuple(bytes.fromhex(segment["changes"][-1]["colour"][1:]))
                    peak = max(expected)
                    expected = tuple(round(v * 255 / peak) for v in expected) if peak else expected
                    observed = seen
                    segment["changes"][-1]["display_rgb"] = list(bytes.fromhex(segment["changes"][-1]["colour"][1:]))
                else:
                    observed = seen
                    expected = (255, 0, 0) if segment["changes"][-1]["colour"] == "red" else (0, 0, 255)
                if observed is None or sum((a - b) ** 2 for a, b in zip(observed, expected)) > 80 ** 2:
                    log["capture_error"] = (
                        f"Capture did not retain the displayed {segment['changes'][-1]['colour']} "
                        f"frame: observed {observed}. Recording is not valid for latency comparison."
                    )
                    print(log["capture_error"], flush=True)
                    finish()
                    return
            if cut >= args.cuts:
                close_segment(segment)
                show(GREEN, f"segment {index + 1}/{len(order)} done")
                root.after(4000, lambda: run_segment(index + 1))
                return
            colour = patches[cut] if args.color_check else (RED if cut % 2 == 0 else BLUE)
            show(colour, f"segment {index + 1}/{len(order)} ({kind})  cut {cut + 1}/{args.cuts}")
            root.update_idletasks()
            segment["changes"].append({"at": time.perf_counter(), "colour": colour if args.color_check else ("red" if colour == RED else "blue")})
            root.after(int(args.hold * 1000), lambda: step(cut + 1))

        step(0)

    try:
        engine.start()
        show(BLACK, f"Start recording. Pattern begins in {args.countdown:.0f} s. Escape stops.")
        root.after(int(args.countdown * 1000), lambda: run_segment(0))
        root.mainloop()
    finally:
        try:
            engine.stop()
        finally:
            if holder["transport"] is not None and log["segments"]:
                close_segment(log["segments"][-1])
            for ip in ips:
                reply = _ask(ip, {"method": "setPilot", "params": _restore_params(priors[ip])})
                print(f"restore {ip}: {reply.get('result')}")
            try:
                runner.run(controller.close_async(), timeout=3.0)
            finally:
                runner.shutdown()

    for number, segment in enumerate(log["segments"], 1):
        line = f"segment {number} ({'link on' if segment['link'] else 'link off'}): {len(segment['changes'])} cuts"
        for link in segment.get("link_stats", []):
            if link.get("confirm_ms") is not None:
                line += (f" | {link['ip']} delivered {link['confirmed']}/{link['updates']}"
                         f" median {link['confirm_ms']:.0f} ms p95 {link['confirm_p95_ms']:.0f} ms"
                         f" awake {link['awake_pct']:.0f}%")
        print(line)
    if args.out:
        args.out.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
        print(f"log: {args.out}")


if __name__ == "__main__":
    main()
